"""Vector search against MariaDB. UNTESTED sketch from MariaDB's RAG tutorial; librarian owns this.

Rules learned in that tutorial (measured there):
- Embed the question with the SAME model as the stored vectors (bge-m3, 1024 dims).
- Put ORDER BY VEC_DISTANCE_COSINE(...) LIMIT n in a SUBQUERY on the vector table, then join.
  A flat join read ~22,000 rows (50 ms) instead of ~200 rows (1 ms).
- Vector index: M=16 and SET mhnsw_ef_search = 80 (defaults were too low).
"""
import os

import pymysql
import requests


def connect():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3307")),
        user=os.getenv("DB_USER", "wikirag"),
        password=os.getenv("DB_PASSWORD", "wikirag"),
        database=os.getenv("DB_NAME", "wikirag"),
        autocommit=True,
    )


def embed_question(text: str) -> list[float]:
    # Ollama embeddings endpoint; verify against `ollama` docs for your version.
    r = requests.post(
        os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/embed",
        json={"model": os.getenv("EMBED_MODEL", "bge-m3"), "input": text},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["embeddings"][0]


SEARCH_SQL = """
SELECT a.lang, a.page_id, a.rev_id, a.title, a.url, a.text, v.distance
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(%s)) AS distance
      FROM article_vector
      ORDER BY distance
      LIMIT %s) AS v
JOIN article a USING (lang, page_id)
ORDER BY v.distance
"""


def search(question: str, limit: int = 10):
    vec = "[" + ",".join(str(x) for x in embed_question(question)) + "]"
    with connect() as con, con.cursor() as cur:
        cur.execute("SET mhnsw_ef_search = 80")
        cur.execute(SEARCH_SQL, (vec, limit))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()], SEARCH_SQL
