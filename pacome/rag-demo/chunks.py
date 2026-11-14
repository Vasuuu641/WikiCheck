"""Section-level chunks of the S articles (fi/sv/en) with bge-m3 vectors. Shared groundwork for the Tier B tools.

  python chunks.py build [--limit N]   split every article on "##", embed, insert; resumable (skips articles already done)
  python chunks.py status

Tables (new, nothing existing is altered):
  chunk(lang, page_id, chunk_no, heading, text)
  chunk_vector(lang, page_id, chunk_no, embedding VECTOR(1024), VECTOR INDEX M=16 DISTANCE=cosine)

The lead (text before the first "##") has heading ''. A section longer than MAX_CHARS is cut at paragraph
boundaries into several chunks that share the heading. Reference-list sections are skipped.
"""
import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import rag

MAX_CHARS = 2500
MIN_CHARS = 80
BATCH = 32
SKIP_HEADINGS = {"lähteet", "viitteet", "aiheesta muualla", "kirjallisuutta", "aiheesta", "katso myös", "huomautukset",
                 "källor", "referenser", "noter", "externa länkar", "litteratur", "se även", "fotnoter",
                 "references", "external links", "further reading", "see also", "notes", "sources", "footnotes", "bibliography",
                 "citations", "general and cited references", "cited sources", "works cited", "notes and references", "general references", "bibliografi", "tryckta källor", "lähdeluettelo", "painetut lähteet"}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS chunk (
  lang CHAR(2) NOT NULL, page_id INT UNSIGNED NOT NULL, chunk_no SMALLINT UNSIGNED NOT NULL,
  heading VARCHAR(300) NOT NULL, text MEDIUMTEXT NOT NULL,
  PRIMARY KEY (lang, page_id, chunk_no)) CHARACTER SET utf8mb4""",
    """CREATE TABLE IF NOT EXISTS chunk_vector (
  lang CHAR(2) NOT NULL, page_id INT UNSIGNED NOT NULL, chunk_no SMALLINT UNSIGNED NOT NULL,
  embedding VECTOR(1024) NOT NULL,
  PRIMARY KEY (lang, page_id, chunk_no),
  VECTOR INDEX (embedding) M=16 DISTANCE=cosine) CHARACTER SET utf8mb4""",
]

_write_lock = threading.Lock()   # concurrent writers to the MHNSW index fail with 'Record has changed since last read'
def is_reference_list(heading, text):
    """True for a bibliography or footnote list, whatever its heading: used to keep such chunks out of the comparisons."""
    if heading.strip().lower() in SKIP_HEADINGS:
        return True
    lines = [l for l in text.split("\n") if l.strip()]
    marks = sum(1 for l in lines if re.match(r"^\s*(\d+\.\s*)?(↑|\^)", l) or l.lstrip().startswith("- ") and re.search(r"\(\d{4}\)|\b\d{4}\)?\.", l))
    return len(lines) >= 4 and marks / len(lines) > 0.5 or text.count("↑") >= 3


_H2 = re.compile(r"^## +(.+?)\s*$", re.M)


def split(text):
    """[(heading, text)] for one article's Markdown. Lead first, then each ## section; long ones cut by paragraph."""
    parts, last, heading = [], 0, ""
    for m in _H2.finditer(text):
        parts.append((heading, text[last:m.start()]))
        heading, last = m.group(1), m.end()
    parts.append((heading, text[last:]))
    out = []
    for i, (h, body) in enumerate(parts):
        if i == 0:                       # drop the "# Title" line from the lead
            body = re.sub(r"^# .*\n?", "", body.strip())
        if h.strip().lower() in SKIP_HEADINGS:
            continue
        cur = ""
        for para in re.split(r"\n\s*\n|\n(?=#{3,} )", body.strip()):
            if cur and len(cur) + len(para) > MAX_CHARS:
                out.append((h, cur))
                cur = ""
            cur = (cur + "\n\n" + para).strip() if cur else para.strip()
            while len(cur) > MAX_CHARS * 1.5:        # one enormous paragraph
                out.append((h, cur[:MAX_CHARS]))
                cur = cur[MAX_CHARS:]
        if cur:
            out.append((h, cur))
    return [(h, t) for h, t in out if len(t) >= MIN_CHARS]


def embed_many(texts):
    """bge-m3 vectors for a list of texts (Ollama /api/embed takes a list). Same model and path as rag.embed."""
    url = rag.OLLAMA
    for attempt in range(4):
        try:
            r = requests.post(f"{url}/api/embed", json={"model": rag.EMBED_MODEL, "input": texts, "keep_alive": "2h"}, timeout=300)
            r.raise_for_status()
            return r.json()["embeddings"]
        except requests.RequestException:
            if attempt == 3:
                raise
            if url != rag.LOCAL_OLLAMA and attempt >= 1:
                url = rag.LOCAL_OLLAMA
            time.sleep(2)


def todo(cur, limit=None):
    cur.execute("SELECT a.lang, a.page_id FROM article a LEFT JOIN (SELECT DISTINCT lang, page_id FROM chunk_vector) c "
                "USING (lang, page_id) WHERE c.page_id IS NULL ORDER BY a.lang, a.page_id" + (f" LIMIT {int(limit)}" if limit else ""))
    return cur.fetchall()


def build(limit=None, workers=2):
    conn = rag.connect()
    cur = conn.cursor()
    for s in SCHEMA:
        cur.execute(s)
    jobs = todo(cur, limit)
    print(f"{len(jobs)} articles without chunks", flush=True)
    t0, done, nchunks = time.time(), 0, 0

    def work(job):
        lang, pid = job
        c = rag.connect()
        try:
            k = c.cursor()
            k.execute("SELECT title, text FROM article WHERE lang = ? AND page_id = ?", (lang, pid))
            title, text = k.fetchone()
            chunks = split(text)
            if not chunks:
                return 0
            # the title in front tells the model what the section is about; the stored text stays clean
            vecs = []
            for i in range(0, len(chunks), BATCH):
                batch = chunks[i:i + BATCH]
                vecs += embed_many([f"{title}\n{h}\n{t}" if h else f"{title}\n{t}" for h, t in batch])
            with _write_lock:
                k.executemany("INSERT INTO chunk VALUES (?,?,?,?,?)", [(lang, pid, n, h, t) for n, (h, t) in enumerate(chunks)])
                k.executemany("INSERT INTO chunk_vector VALUES (?,?,?,VEC_FromText(?))", [(lang, pid, n, str(v)) for n, v in enumerate(vecs)])
                c.commit()
            return len(chunks)
        finally:
            c.close()

    with ThreadPoolExecutor(workers) as pool:
        for n in pool.map(work, jobs):
            done += 1
            nchunks += n
            if done % 200 == 0:
                rate = done / (time.time() - t0)
                print(f"{done}/{len(jobs)} articles, {nchunks} chunks, {rate:.1f} art/s, ETA {(len(jobs) - done) / rate / 60:.0f} min", flush=True)
    print(f"done: {done} articles, {nchunks} chunks", flush=True)


def status():
    conn = rag.connect()
    cur = conn.cursor()
    cur.execute("SELECT lang, COUNT(*), COUNT(DISTINCT page_id) FROM chunk GROUP BY lang")
    print(cur.fetchall())
    cur.execute("SELECT COUNT(*) FROM chunk_vector")
    print("vectors:", cur.fetchone()[0])


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["build", "status"])
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=2)
    a = p.parse_args()
    build(a.limit, a.workers) if a.cmd == "build" else status()
