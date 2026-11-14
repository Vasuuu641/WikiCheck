"""Retrieval-augmented answers over Finnish/Swedish/English Wikipedia, with MariaDB Vector.

Pipeline: question -> bge-m3 vector (Ollama) -> one SQL statement on MariaDB -> top articles
-> Gemini writes a short answer that cites them as [1], [2], ...

Settings come from ../.env or the environment:
  GOOGLE_API_KEY   required for the answer step
  GEMINI_MODEL     default gemini-3.8-flash (gemini-2.5-flash is closed to new API keys)
  DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME   default 127.0.0.1:3306 wikirag/wikirag
  OLLAMA_URL       default http://localhost:11434
"""
import json
import os
import re
import time

import mariadb
import requests


def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

DB = dict(host=os.environ.get("DB_HOST", "127.0.0.1"),
          port=int(os.environ.get("DB_PORT", 3306)),
          user=os.environ.get("DB_USER", "wikirag"),
          password=os.environ.get("DB_PASSWORD", "wikirag"),
          database=os.environ.get("DB_NAME", "wikirag"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

THINKING = os.environ.get("GEMINI_THINKING", "low")   # minimal/low/medium/high: less thinking = faster first token
EMBED_MODEL = "bge-m3"
EF_SEARCH = 80            # the starter measured 100% recall@10 at M=16, ef_search=80
CANDIDATES = 12           # articles the vector search returns
MAX_CHARS = 6000          # per article sent to the LLM (articles can be 100 000+ characters)

# The query shape matters: vector search in a subquery so MariaDB uses the vector index,
# join to the article afterwards (see the starter README, "Why the subquery").
SEARCH_SQL = """\
SELECT a.lang, a.title, a.url, a.wikidata_id, v.distance, a.text, a.page_id
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS distance
      FROM article_vector
      {where}
      ORDER BY distance
      LIMIT ?) AS v
JOIN article a USING (lang, page_id)
ORDER BY v.distance"""

SYSTEM = (
    "You answer questions about the history of Finland using ONLY the numbered Wikipedia "
    "sources provided. Cite the sources you use as [1], [2] directly after each claim. "
    "If the sources do not contain the answer, say that they do not, and do not use outside "
    "knowledge. Reply in the language of the question. Be concise: at most about 120 words. "
    "Write plain text in short paragraphs. Do not use markdown headings or tables. "
    "You may use **bold** sparingly and simple '- ' bullet lists."
)


def connect():
    return mariadb.connect(**DB)


LOCAL_OLLAMA = "http://localhost:11434"


def _embed_at(url, text, timeout):
    r = requests.post(f"{url}/api/embed", json={"model": EMBED_MODEL, "input": text, "keep_alive": "2h"}, timeout=timeout)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def embed(text):
    """Embed with OLLAMA_URL (e.g. the Verda GPU through scripts/verda.py tunnel); fall back to local Ollama."""
    try:
        return _embed_at(OLLAMA, text, 120 if OLLAMA == LOCAL_OLLAMA else 15)
    except requests.RequestException:
        if OLLAMA == LOCAL_OLLAMA:
            raise
        return _embed_at(LOCAL_OLLAMA, text, 120)


def timed_embed(question):
    t0 = time.perf_counter()
    vector = embed(question)
    return vector, (time.perf_counter() - t0) * 1000


def search(vector, lang=None, sources=5):
    """Return (articles, search_ms, sql). One article per topic (wikidata_id), best version first."""
    where = "WHERE lang = ?" if lang else ""
    sql = SEARCH_SQL.format(where=where)
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


def retrieve(question, lang=None, sources=5):
    """Return (articles, timings, sql)."""
    vector, t_embed = timed_embed(question)
    articles, t_search, sql = search(vector, lang, sources)
    return articles, dict(embed_ms=t_embed, search_ms=t_search), sql


def _request_body(question, articles):
    blocks = []
    for i, a in enumerate(articles, 1):
        text = a["text"][:MAX_CHARS]
        blocks.append(f"[{i}] {a['title']} ({a['lang']}.wikipedia.org)\n{text}")
    prompt = "SOURCES\n\n" + "\n\n---\n\n".join(blocks) + f"\n\nQUESTION\n{question}"
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600, "thinkingConfig": {"thinkingLevel": THINKING}},
    }


def _key():
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")   # the API in ../../api uses the second name
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is not set (put it in pacome/.env)")
    return key


def generate_stream(question, articles):
    """Yield the answer text in chunks as Gemini produces it."""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:streamGenerateContent?alt=sse",
        headers={"x-goog-api-key": _key()}, json=_request_body(question, articles), timeout=120, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini returned {r.status_code}: {r.text[:300]}")
    r.encoding = "utf-8"
    got = False
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = json.loads(line[5:])
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            continue
        text = "".join(p.get("text", "") for p in parts)
        if text:
            got = True
            yield text
    if not got:
        raise RuntimeError("Gemini gave no answer")


def generate(question, articles):
    """Ask Gemini for a short answer grounded in the articles. Returns (answer, ms)."""
    t0 = time.perf_counter()
    answer = "".join(generate_stream(question, articles)).strip()
    return answer, (time.perf_counter() - t0) * 1000


def cited(answer):
    """The source numbers the answer actually cites."""
    return sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})


def ask(question, lang=None, sources=5):
    articles, timings, sql = retrieve(question, lang, sources)
    answer, gen_ms = generate(question, articles)
    timings["generate_ms"] = gen_ms
    return dict(question=question, answer=answer, articles=articles, timings=timings, sql=sql)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Ask Wikipedia (history of Finland) a question")
    p.add_argument("question")
    p.add_argument("--lang", choices=["fi", "sv", "en"], help="only search articles in this language")
    p.add_argument("--sources", type=int, default=5)
    args = p.parse_args()
    res = ask(args.question, args.lang, args.sources)
    print(res["answer"], "\n")
    for i, a in enumerate(res["articles"], 1):
        print(f"[{i}] {a['title']} ({a['lang']}, distance {a['distance']:.3f}) {a['url']}")
    t = res["timings"]
    print(f"\nembed {t['embed_ms']:.0f} ms | MariaDB {t['search_ms']:.0f} ms | Gemini {t['generate_ms']:.0f} ms")
