"""Vector search against MariaDB, adapted from MariaDB's RAG tutorial; the librarian owns this.

Rules learned in that tutorial (measured there):
- Embed the question with the SAME model as the stored vectors (bge-m3, 1024 dims).
- Put ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n in a SUBQUERY on the vector table, then join.
  A flat join read ~22,000 rows (50 ms) instead of ~200 rows (1 ms).
- Vector index: M=16 and SET mhnsw_ef_search = 80 (defaults were too low).
"""
import json
import os

import pymysql
import requests

# Same options the tutorial used to embed S: Ollama needs num_ctx AND num_batch raised,
# otherwise texts over 2,048 tokens are refused or cut.
OLLAMA_OPTIONS = {"num_ctx": 8192, "num_batch": 8192}


def connect():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3307")),
        user=os.getenv("DB_USER", "wikirag"),
        password=os.getenv("DB_PASSWORD", "wikirag"),
        database=os.getenv("DB_NAME", "wikirag"),
        autocommit=True,
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed several texts in one Ollama call (bge-m3, same as the stored vectors)."""
    r = requests.post(
        os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/embed",
        json={
            "model": os.getenv("EMBED_MODEL", "bge-m3"),
            "input": texts,
            "truncate": True,
            "options": OLLAMA_OPTIONS,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["embeddings"]


def embed_question(text: str) -> list[float]:
    return embed_texts([text])[0]


SEARCH_SQL = """
SELECT a.lang, a.page_id, a.rev_id, a.wikidata_id, a.title, a.url, v.distance{text}
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(%s)) AS distance
      FROM article_vector
      ORDER BY distance
      LIMIT %s) AS v
JOIN article a USING (lang, page_id)
ORDER BY v.distance
"""
SEARCH_SQL_TEXT = SEARCH_SQL.format(text=", a.text")   # with the article text (GET /api/search)
SEARCH_SQL = SEARCH_SQL.format(text="")


# Chunk-first: the nearest paragraphs (chunk_vector, eval/build_chunks.py) instead of whole
# articles. Same shape as SEARCH_SQL: vector index in the subquery, then the joins. Chunks exist
# only for Finnish articles (chunk has no lang column), so the join is to the fi article row.
CHUNK_SEARCH_SQL = """
SELECT c.page_id, c.chunk_no, c.rev_id, c.text, a.wikidata_id, a.title, a.url, v.distance
FROM (SELECT page_id, chunk_no, VEC_DISTANCE_COSINE(embedding, VEC_FromText(%s)) AS distance
      FROM chunk_vector
      ORDER BY distance
      LIMIT %s) AS v
JOIN chunk c USING (page_id, chunk_no)
JOIN article a ON a.lang = 'fi' AND a.page_id = v.page_id
ORDER BY v.distance
"""


def search_chunks(vector: list[float], limit: int = 40):
    """Nearest paragraphs to the question vector. Returns (rows, sql)."""
    with connect() as con, con.cursor() as cur:
        cur.execute("SET mhnsw_ef_search = %s", (max(80, limit),))
        cur.execute(CHUNK_SEARCH_SQL, (json.dumps(vector), limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()], CHUNK_SEARCH_SQL


def articles_from_chunks(chunks: list[dict]) -> list[dict]:
    """Distinct articles in the order of their best chunk (an article ranks where its best
    paragraph ranks); `distance` is that best chunk's."""
    seen, out = set(), []
    for c in chunks:
        if c["page_id"] not in seen:
            seen.add(c["page_id"])
            out.append({"lang": "fi", "page_id": c["page_id"], "wikidata_id": c["wikidata_id"],
                        "title": c["title"], "url": c["url"], "distance": c["distance"]})
    return out


RRF_K = 60            # reciprocal rank fusion constant: score = 1/(K + rank in A) + 1/(K + rank in B)
FUSION_LIST = 20      # each list contributes its top 20 articles
FUSION_CHUNKS = 100   # nearest chunks fetched to get ~20 distinct articles for list B


def _topic(row: dict):
    """One key per topic: the fi, sv and en articles on one subject share a wikidata_id."""
    return row.get("wikidata_id") or (row["lang"], row["page_id"])


def rrf_merge(lists: list[list[dict]]) -> list[dict]:
    """Reciprocal rank fusion of several article lists (each best first). Each list ranks topics
    (other languages of a topic already in that list are skipped); score = sum over the lists of
    1/(RRF_K + rank), a topic missing from a list gets nothing from it. Ties go to the better
    rank in the first list. Returns the rows best first, each with `rrf` and `ranks` (one rank or
    None per list); a topic's row is its Finnish article when any list has it."""
    merged: dict = {}
    for i, rows in enumerate(lists):
        rank = 0
        for r in rows:
            entry = merged.setdefault(_topic(r), {"row": r, "ranks": [None] * len(lists)})
            if entry["ranks"][i] is not None:
                continue
            rank += 1
            entry["ranks"][i] = rank
            if r["lang"] == "fi" and entry["row"]["lang"] != "fi":
                entry["row"] = r
    out = [{**e["row"], "rrf": sum(1 / (RRF_K + k) for k in e["ranks"] if k is not None),
            "ranks": e["ranks"]} for e in merged.values()]
    out.sort(key=lambda r: (-r["rrf"], r["ranks"][0] or 10**6))
    return out


def fused_search(vector: list[float], limit: int = 10):
    """RETRIEVAL=fusion: reciprocal rank fusion of two article lists.
      A: article-first, the 20 nearest article vectors (any language), one entry per topic.
      B: chunk-first, articles ranked by their best paragraph in chunk_vector, top 20.
    score = 1/(RRF_K + rank in A) + 1/(RRF_K + rank in B); an article missing from a list gets
    nothing from it. Ties go to the better rank in A. Returns (rows, sql) like search(); each row
    also has `rrf`, `rank_a` and `rank_b` (None when not in that list)."""
    a_rows, a_sql = search("", limit=FUSION_LIST, vector=vector)
    b_rows = articles_from_chunks(search_chunks(vector, limit=FUSION_CHUNKS)[0])[:FUSION_LIST]
    out = [{**r, "rank_a": r["ranks"][0], "rank_b": r["ranks"][1]} for r in rrf_merge([a_rows, b_rows])]
    sql =("-- RETRIEVAL=fusion: reciprocal rank fusion (k = 60) of two lists, merged in Python.\n"
           "-- A: the 20 nearest articles\n" + a_sql.strip() +
           f"\n\n-- B: the {FUSION_CHUNKS} nearest paragraphs, grouped by article (top 20)\n" + CHUNK_SEARCH_SQL.strip())
    return out[:limit], sql


def search(question: str, limit: int = 10, vector: list[float] | None = None, with_text: bool = False):
    """Nearest articles to the question. Returns (rows, sql). Pass `vector` to skip embedding;
    with_text=True also returns each article's text."""
    vec = json.dumps(vector if vector is not None else embed_question(question))
    sql = SEARCH_SQL_TEXT if with_text else SEARCH_SQL
    with connect() as con, con.cursor() as cur:
        cur.execute("SET mhnsw_ef_search = 80")
        cur.execute(sql, (vec, limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()], sql
