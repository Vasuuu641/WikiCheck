"""Database access for opinion drift: a connection, and the nearest articles to a question.

Same settings as the rest of the API (DB_* in .env). Unlike api/search.connect(), a connection here
starts with autocommit off: loading an article's history is one transaction, so a half-loaded
history is never taken for a loaded one. Code that wants autocommit turns it on.
"""
import os
import time

import pymysql

from ..search import embed_question

EF_SEARCH = 80            # MariaDB's tutorial measured 100% recall@10 at M=16, ef_search=80
CANDIDATES = 12           # articles the vector search returns

# Vector search in a subquery so MariaDB uses the vector index, then join to the article.
SEARCH_SQL = """\
SELECT a.lang, a.title, a.url, a.wikidata_id, v.distance, a.text, a.page_id
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(%s)) AS distance
      FROM article_vector
      {where}
      ORDER BY distance
      LIMIT %s) AS v
JOIN article a USING (lang, page_id)
ORDER BY v.distance"""


def connect():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "wikirag"),
        password=os.getenv("DB_PASSWORD", "wikirag"),
        database=os.getenv("DB_NAME", "wikirag"),
        autocommit=False,
    )


def timed_embed(question):
    t0 = time.perf_counter()
    vector = embed_question(question)
    return vector, (time.perf_counter() - t0) * 1000


def search(vector, lang=None, sources=5):
    """Return (articles, search_ms, sql). One article per topic (wikidata_id), best version first."""
    sql = SEARCH_SQL.format(where="WHERE lang = %s" if lang else "")
    params = [str(vector)] + ([lang] if lang else []) + [CANDIDATES]
    t0 = time.perf_counter()
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SET SESSION mhnsw_ef_search = {EF_SEARCH}")
        cur.execute(sql, params)
        rows = cur.fetchall()
    finally:
        conn.close()
    t_search = (time.perf_counter() - t0) * 1000

    seen, articles = set(), []
    for l, title, url, wikidata_id, distance, text, page_id in rows:
        key = wikidata_id or (l, title)
        if key in seen:
            continue
        seen.add(key)
        articles.append(dict(lang=l, title=title, url=url, distance=float(distance), text=text,
                             page_id=page_id, wikidata_id=wikidata_id))
        if len(articles) == sources:
            break
    return articles, t_search, sql
