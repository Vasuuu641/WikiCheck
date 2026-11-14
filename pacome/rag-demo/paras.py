"""Finnish paragraph layer from the NEWEST version of each article in dataset M (raw wikitext). Groundwork for B2 and B3.

  python paras.py build [--root "Suomen sodat"] [--limit N]   resumable: skips articles that already have paragraphs
  python paras.py status

New tables only:
  para(page_id, para_no, heading, text, has_ref)      cleaned paragraph (history.clean), one row per paragraph >= MIN_CHARS
  para_vector(page_id, para_no, embedding)            bge-m3, VECTOR INDEX M=16 cosine
  para_ref(page_id, para_no, ref_text)                one row per <ref> in the paragraph (named reuses resolved)
  article_link(from_page_id, to_title)                outgoing [[links]] of the article, namespaces and interlanguage links dropped

The newest wikitext is read straight from data/m/*.parquet (the highest timestamp per page), so nothing is added to the
system-versioned article_version table.
"""
import argparse
import os
import re
import time

import pyarrow.parquet as pq

import chunks
import history
import rag

MIN_CHARS = 80
MAX_EMBED_CHARS = 2500
BATCH = 32

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS para (
  page_id INT UNSIGNED NOT NULL, para_no SMALLINT UNSIGNED NOT NULL, heading VARCHAR(300) NOT NULL,
  text MEDIUMTEXT NOT NULL, has_ref BOOL NOT NULL, PRIMARY KEY (page_id, para_no)) CHARACTER SET utf8mb4""",
    """CREATE TABLE IF NOT EXISTS para_vector (
  page_id INT UNSIGNED NOT NULL, para_no SMALLINT UNSIGNED NOT NULL, embedding VECTOR(1024) NOT NULL,
  PRIMARY KEY (page_id, para_no), VECTOR INDEX (embedding) M=16 DISTANCE=cosine) CHARACTER SET utf8mb4""",
    """CREATE TABLE IF NOT EXISTS para_ref (
  page_id INT UNSIGNED NOT NULL, para_no SMALLINT UNSIGNED NOT NULL, ref_no SMALLINT UNSIGNED NOT NULL,
  ref_text TEXT NOT NULL, PRIMARY KEY (page_id, para_no, ref_no)) CHARACTER SET utf8mb4""",
    """CREATE TABLE IF NOT EXISTS article_link (
  from_page_id INT UNSIGNED NOT NULL, to_title VARCHAR(255) NOT NULL,
  PRIMARY KEY (from_page_id, to_title), KEY (to_title)) CHARACTER SET utf8mb4""",
]

SUBTREE_SQL = """\
WITH RECURSIVE sub(cat) AS (SELECT CAST(? AS CHAR(255)) UNION SELECT child FROM category_link JOIN sub ON parent = cat)
SELECT DISTINCT ac.page_id FROM sub JOIN article_category ac ON ac.category = sub.cat AND ac.lang = 'fi'"""

# ---------------------------------------------------------------- wikitext parsing

_REF_FULL = re.compile(r"<ref(\s[^>]*?)?(?<!/)>(.*?)</ref>", re.S | re.I)
_REF_EMPTY = re.compile(r"<ref(\s[^>]*?)/>", re.I)
_NAME = re.compile(r"""name\s*=\s*["']?([^"'\s/>]+)""", re.I)
_KEYS = ("tekijä", "author", "last", "otsikko", "title", "nimeke", "nimi", "julkaisija", "publisher", "julkaisu", "work",
         "sivusto", "teos", "url", "osoite", "julkaisupaikka", "vuosi", "year", "julkaisuaika", "date", "ajankohta", "sivu", "sivut", "page", "pages", "isbn")
_LINK = re.compile(r"\[\[([^\]|#\[]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_NS = re.compile(r"^(tiedosto|kuva|file|image|luokka|category|mediawiki|malline|template|ohje|help|wikipedia|toiminnot|käyttäjä|user|"
                 r"keskustelu|talk|wikipedia-keskustelu|[a-z\-]{2,12}):", re.I)


def _ref_text(raw):
    """Readable text for one <ref> body: template fields (title, publisher, url ...) joined, or the stripped text."""
    raw = raw.strip()
    m = re.match(r"^\{\{\s*([^|{}]*)\|(.*)\}\}\s*$", raw, re.S)
    if m:
        fields = {}
        for part in re.split(r"\|(?=\s*[\w\säö\-]+=)", m.group(2)):
            if "=" in part:
                k, v = part.split("=", 1)
                v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v).strip()
                if v:
                    fields.setdefault(k.strip().lower(), v)
        picked = [fields[k] for k in _KEYS if k in fields]
        if picked:
            return f"{m.group(1).strip()}: " + "; ".join(dict.fromkeys(picked))
    t = re.sub(r"\{\{[^{}]*\}\}", "", raw)
    t = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[(https?://\S+)(?: ([^\]]*))?\]", lambda m: (m.group(2) or "") + " " + m.group(1), t)
    return re.sub(r"<[^>]+>|'''?", "", t).strip() or raw[:300]


def parse(wikitext):
    """-> (paragraphs, links). paragraphs: [{heading, text, refs: [str]}]; links: set of normalised target titles."""
    links = set()
    for m in _LINK.finditer(re.sub(r"<!--.*?-->", "", wikitext, flags=re.S)):
        t = m.group(1).strip().replace("_", " ")
        if t and not _NS.match(t):
            links.add(t[0].upper() + t[1:])

    refs, named = [], {}

    def keep(m):
        body = _ref_text(m.group(2))
        nm = _NAME.search(m.group(1) or "")
        if nm:
            named[nm.group(1)] = body
        refs.append(body)
        return f"\x01{len(refs) - 1}\x01"

    def reuse(m):
        nm = _NAME.search(m.group(1) or "")
        if nm and nm.group(1) in named:
            refs.append(named[nm.group(1)])
            return f"\x01{len(refs) - 1}\x01"
        return ""

    t = re.sub(r"<!--.*?-->", "", wikitext, flags=re.S)
    t = _REF_FULL.sub(keep, t)
    t = _REF_EMPTY.sub(reuse, t)            # <ref name="x" /> after its definition; unresolved ones are dropped
    cleaned = history.clean(t)

    out, heading = [], ""
    for block in cleaned.split("\n\n"):
        if block.startswith("## "):
            heading = block[3:].strip()
            continue
        if block.startswith("- ") or heading.lower() in chunks.SKIP_HEADINGS:
            continue
        idx = [int(x) for x in re.findall(r"\x01(\d+)\x01", block)]
        text = re.sub(r"\x01\d+\x01", "", block).strip()
        if len(text) >= MIN_CHARS:
            out.append(dict(heading=heading, text=text, refs=[refs[i] for i in idx]))
    return out, links


# ---------------------------------------------------------------- newest wikitext from M

def newest_wikitext(page_ids):
    """{page_id: wikitext} of the latest revision of each page, read from data/m/*.parquet."""
    idx = history._m_index()
    by_file = {}
    for pid in page_ids:
        for f in idx.get(pid, []):
            by_file.setdefault(f, []).append(pid)
    best = {}
    for f, pids in sorted(by_file.items()):
        t = pq.read_table(os.path.join(history.M_DIR, f), filters=[("page_id", "in", pids)],
                          columns=["page_id", "rev_id", "timestamp", "text"])
        for pid, rev, ts, text in zip(*(t.column(c).to_pylist() for c in ("page_id", "rev_id", "timestamp", "text"))):
            if text and (pid not in best or (ts, rev) > best[pid][0]):
                best[pid] = ((ts, rev), text)
    return {pid: v[1] for pid, v in best.items()}


# ---------------------------------------------------------------- build

def build(root, limit=None, group=40):
    conn = rag.connect()
    cur = conn.cursor()
    for s in SCHEMA:
        cur.execute(s)
    cur.execute(SUBTREE_SQL, (root,))
    pids = sorted(r[0] for r in cur.fetchall())
    cur.execute("SELECT DISTINCT page_id FROM para")
    have = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT from_page_id FROM article_link")
    have |= {r[0] for r in cur.fetchall()}
    inm = set(history._m_index())
    todo = [p for p in pids if p not in have and p in inm]
    if limit:
        todo = todo[:limit]
    print(f"{root}: {len(pids)} fi articles, {len(todo)} to do ({len(pids) - len([p for p in pids if p in inm])} without history in M)", flush=True)
    t0, npara = time.time(), 0
    for g in range(0, len(todo), group):
        part = todo[g:g + group]
        texts = newest_wikitext(part)
        cur.execute("SELECT page_id, title FROM article WHERE lang='fi' AND page_id IN (%s)" % ",".join(map(str, part)))
        titles = dict(cur.fetchall())
        rows, links = [], []
        for pid in part:
            if pid not in texts:
                continue
            paras, lk = parse(texts[pid])
            rows += [(pid, n, p) for n, p in enumerate(paras)]
            links += [(pid, t[:255]) for t in lk]
        vecs = []
        for i in range(0, len(rows), BATCH):
            vecs += chunks.embed_many([p["text"][:MAX_EMBED_CHARS] for _, _, p in rows[i:i + BATCH]])
        cur.executemany("INSERT IGNORE INTO article_link VALUES (?,?)", links)
        cur.executemany("INSERT INTO para VALUES (?,?,?,?,?)", [(pid, n, p["heading"], p["text"], bool(p["refs"])) for pid, n, p in rows])
        for i in range(0, len(rows), 64):          # a whole group of vectors as one statement would exceed max_allowed_packet (16 MB)
            cur.executemany("INSERT INTO para_vector VALUES (?,?,VEC_FromText(?))",
                            [(pid, n, str(v)) for (pid, n, _), v in zip(rows[i:i + 64], vecs[i:i + 64])])
        cur.executemany("INSERT INTO para_ref VALUES (?,?,?,?)", [(pid, n, k, r[:2000]) for pid, n, p in rows for k, r in enumerate(p["refs"])])
        conn.commit()
        npara += len(rows)
        print(f"{g + len(part)}/{len(todo)} articles, {npara} paragraphs, {(time.time() - t0) / 60:.1f} min", flush=True)


def status():
    cur = rag.connect().cursor()
    for tbl in ("para", "para_vector", "para_ref", "article_link"):
        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
        print(tbl, cur.fetchone()[0])
    cur.execute("SELECT SUM(has_ref), COUNT(*) FROM para")
    print("with ref / all:", cur.fetchone())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["build", "status"])
    p.add_argument("--root", default="Suomen sodat")
    p.add_argument("--limit", type=int)
    a = p.parse_args()
    build(a.root, a.limit) if a.cmd == "build" else status()
