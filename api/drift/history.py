"""Edit history of the Finnish articles (dataset M), kept in a MariaDB system-versioned table.

One row per article in `article_version`; every earlier version is a history row whose [row_start, row_end)
is the time that version was live on Wikipedia. Articles are loaded on first use from dataset M's parquet
files in HISTORY_DIR (default pacome/data/m; pacome/scripts/load_m.py downloads them).

  resolve_fi(article)            Finnish page_id of a search result's topic, or None
  ensure_loaded(page_id)         idempotent load of one article's versions
  revisions(page_id)             [{rev_id, t, user, anonymous, comment, bytes, sha1, reverted, is_revert}], oldest first
  wars(page_id)                  [[t0, t1]] months with 3+ reverts between named users
  activity(page_id)              [["YYYY-MM", edits, reverts]]
  text(page_id, rev_id)          cleaned text, at most MAX_CHARS
  text_asof(page_id, iso)        (rev_id, text, sql)
  hunks(page_id, rev_a, rev_b)   {"del": [...], "add": [...]} paragraphs removed and added

The wikitext cleaner follows history.py in MariaDB/mariadb-system-versioning-wikipedia (MIT licence).
"""
import datetime as dt
import difflib
import functools
import os
import re
import threading

from . import store

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
M_DIR = os.getenv("HISTORY_DIR") or os.path.join(ROOT, "pacome", "data", "m")
MAX_CHARS = 16000
BATCH_BYTES = 6_000_000

SCHEMA = """\
CREATE TABLE IF NOT EXISTS article_version (
  page_id     INT UNSIGNED NOT NULL,
  title       VARCHAR(255) NOT NULL,
  rev_id      INT UNSIGNED NOT NULL,
  user        VARCHAR(255),
  anonymous   BOOL NOT NULL DEFAULT 0,
  comment     TEXT,
  minor       BOOL NOT NULL DEFAULT 0,
  sha1        CHAR(40),
  len         INT UNSIGNED,
  text        MEDIUMTEXT COMPRESSED NOT NULL,
  row_start   TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end     TIMESTAMP(6) GENERATED ALWAYS AS ROW END INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id),
  KEY start_key (page_id, row_start),
  KEY rev_key (rev_id)
) CHARACTER SET utf8mb4 WITH SYSTEM VERSIONING"""

AS_OF_SQL = """\
SELECT rev_id, row_start, text
FROM article_version FOR SYSTEM_TIME AS OF TIMESTAMP %s
WHERE page_id = %s"""

VERSIONS_SQL = """\
SELECT rev_id, row_start, user, anonymous, comment, len, sha1
FROM article_version FOR SYSTEM_TIME ALL
WHERE page_id = %s
ORDER BY row_start"""

_load_lock = threading.Lock()
_index = None


def _conn():
    conn = store.connect()
    conn.cursor().execute("SET time_zone = '+00:00'")
    return conn


def _m_index():
    """{page_id: [file, ...]} from data/m/index.parquet."""
    global _index
    if _index is None:
        import pyarrow.parquet as pq
        path = os.path.join(M_DIR, "index.parquet")
        if not os.path.exists(path):
            raise RuntimeError(f"{M_DIR}/index.parquet is missing: run pacome/scripts/load_m.py")
        t = pq.read_table(path, columns=["page_id", "file"])
        idx = {}
        for pid, f in zip(t.column(0).to_pylist(), t.column(1).to_pylist()):
            idx.setdefault(pid, []).append(f)
        _index = idx
    return _index


def resolve_fi(article):
    """Finnish page_id for a store.search() result (same topic by wikidata_id), or None if M has no history for it."""
    pid = None
    if article.get("lang") == "fi":
        pid = article.get("page_id")
    elif article.get("wikidata_id"):
        conn = store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT page_id FROM article WHERE lang = 'fi' AND wikidata_id = %s", (article["wikidata_id"],))
            row = cur.fetchone()
            pid = row[0] if row else None
        finally:
            conn.close()
    return pid if pid is not None and pid in _m_index() else None


def ensure_loaded(page_id):
    """Load one article's versions with their real timestamps. Returns the number of versions inserted (0 if present)."""
    with _load_lock:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(SCHEMA)
            cur.execute("SELECT 1 FROM article_version WHERE page_id = %s", (page_id,))
            if cur.fetchone():
                return 0
            import pyarrow.parquet as pq
            rows = []
            for f in sorted(_m_index().get(page_id, [])):
                t = pq.read_table(os.path.join(M_DIR, f), filters=[("page_id", "=", page_id)],
                                  columns=["title", "rev_id", "timestamp", "user", "anonymous", "comment", "minor", "sha1", "bytes", "text"])
                rows.extend(t.to_pylist())
            if not rows:
                return 0
            rows.sort(key=lambda r: (r["timestamp"], r["rev_id"]))
            starts, prev = [], None
            for r in rows:                      # two edits in the same second: shift the later one by a microsecond
                ts = r["timestamp"].replace(tzinfo=None)
                if prev is not None and ts <= prev:
                    ts = prev + dt.timedelta(microseconds=1)
                starts.append(ts)
                prev = ts
            fmt = lambda ts: ts.strftime("%Y-%m-%d %H:%M:%S.%f")
            cur.execute("SET system_versioning_insert_history = 1")
            hist_sql = ("INSERT INTO article_version (page_id, title, rev_id, user, anonymous, comment, minor, sha1, len, text, "
                        "row_start, row_end) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
            batch, size = [], 0
            for i, r in enumerate(rows[:-1]):
                text = r["text"] or ""
                batch.append((page_id, r["title"], r["rev_id"], r["user"], bool(r["anonymous"]), r["comment"], bool(r["minor"]),
                              r["sha1"], r["bytes"], text, fmt(starts[i]), fmt(starts[i + 1])))
                size += len(text.encode("utf-8")) + 400
                if size > BATCH_BYTES:
                    cur.executemany(hist_sql, batch)
                    batch, size = [], 0
            if batch:
                cur.executemany(hist_sql, batch)
            r = rows[-1]
            cur.execute("INSERT INTO article_version (page_id, title, rev_id, user, anonymous, comment, minor, sha1, len, text, "
                        "row_start) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (page_id, r["title"], r["rev_id"], r["user"], bool(r["anonymous"]), r["comment"], bool(r["minor"]),
                         r["sha1"], r["bytes"], r["text"] or "", fmt(starts[-1])))
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


@functools.lru_cache(maxsize=64)
def _revisions(page_id):
    ensure_loaded(page_id)
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(VERSIONS_SQL, (page_id,))
        rows = cur.fetchall()
    finally:
        conn.close()
    revs = [dict(rev_id=r[0], t=_iso(r[1]), ts=r[1], user=r[2], anonymous=bool(r[3]), comment=r[4] or "", bytes=r[5] or 0,
                 sha1=r[6], reverted=False, is_revert=False, undid_named=False) for r in rows]
    last_seen = {}
    for j, v in enumerate(revs):
        i = last_seen.get(v["sha1"])
        if i is not None and i < j - 1 and v["sha1"] != revs[j - 1]["sha1"]:
            v["is_revert"] = True                     # puts back the text of version i, undoing i+1 .. j-1
            v["undid_named"] = not v["anonymous"] and not revs[j - 1]["anonymous"]
            if (v["ts"] - revs[i + 1]["ts"]).total_seconds() <= 86400:
                for k in range(i + 1, j):
                    revs[k]["reverted"] = True
        last_seen[v["sha1"]] = j
    return revs


def revisions(page_id):
    keys = ("rev_id", "t", "user", "anonymous", "comment", "bytes", "sha1", "reverted", "is_revert")
    return [{k: v[k] for k in keys} for v in _revisions(page_id)]


def activity(page_id):
    months = {}
    for v in _revisions(page_id):
        m = months.setdefault(v["t"][:7], [0, 0])
        m[0] += 1
        m[1] += v["is_revert"]
    return [[k, e, r] for k, (e, r) in sorted(months.items())]


def wars(page_id):
    """Months with 3 or more reverts where a named editor undid a named editor."""
    months = {}
    for v in _revisions(page_id):
        if v["is_revert"] and v["undid_named"]:
            months[v["t"][:7]] = months.get(v["t"][:7], 0) + 1
    out = []
    for m, n in sorted(months.items()):
        if n >= 3:
            y, mo = int(m[:4]), int(m[5:7])
            end = f"{y + (mo == 12):04d}-{mo % 12 + 1:02d}"
            out.append([f"{m}-01T00:00:00Z", f"{end}-01T00:00:00Z"])
    return out


# ---------------------------------------------------------------- wikitext -> readable text

_TOKEN = re.compile(r"\{\{|\}\}|\{\||\|\}")


def _drop_nested(text):
    """Remove {{templates}} and {| tables |}, nested."""
    out, depth, pos = [], 0, 0
    for m in _TOKEN.finditer(text):
        tok = m.group()
        if tok in ("{{", "{|"):
            if depth == 0:
                out.append(text[pos:m.start()])
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                pos = m.end()
        # a stray closer at depth 0 stays in the text
    if depth == 0:
        out.append(text[pos:])
    return "".join(out)


def clean(wikitext):
    """Wikitext -> plain paragraphs separated by blank lines. Headings are kept as '## Heading'."""
    t = re.sub(r"<!--.*?-->", "", wikitext, flags=re.S)
    t = re.sub(r"<ref[^>/]*/>", "", t)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    t = _drop_nested(t)
    t = re.sub(r"\[\[(?:Tiedosto|Kuva|File|Image|Luokka|Category):[^\[\]]*(?:\[\[[^\]]*\]\][^\[\]]*)*\]\]", "", t, flags=re.I)
    t = re.sub(r"\[\[[a-z\-]{2,12}:[^\]]*\]\]", "", t)             # interlanguage links
    t = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", t)
    t = re.sub(r"\[https?://\S+\]", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    blocks = []
    for line in t.split("\n"):
        line = line.strip()
        if not line:
            continue
        heading = re.match(r"^(=+)\s*(.*?)\s*=+$", line)
        if heading:
            blocks.append("## " + heading.group(2))
        elif line[0] in "*#":
            blocks.append("- " + line.lstrip("*#: "))
        elif line[0] not in "|!:;":
            blocks.append(line)
    return "\n\n".join(blocks)


@functools.lru_cache(maxsize=2048)
def _clean_rev(page_id, rev_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT text FROM article_version FOR SYSTEM_TIME ALL WHERE page_id = %s AND rev_id = %s", (page_id, rev_id))
        row = cur.fetchone()
    finally:
        conn.close()
    return clean(row[0]) if row else ""


def full_text(page_id, rev_id):
    ensure_loaded(page_id)
    return _clean_rev(page_id, rev_id)


def text(page_id, rev_id):
    """Cleaned text of one version, cut at a paragraph break under MAX_CHARS."""
    t = full_text(page_id, rev_id)
    if len(t) > MAX_CHARS:
        cut = t.rfind("\n\n", 0, MAX_CHARS)
        t = t[:cut if cut > MAX_CHARS // 2 else MAX_CHARS]
    return t


def text_asof(page_id, iso):
    """The article as it stood at a moment (UTC). Returns (rev_id, cleaned text, sql) or (None, '', sql)."""
    ensure_loaded(page_id)
    moment = str(iso).replace("T", " ").rstrip("Z")[:19]
    moment += {10: " 00:00:00", 16: ":00"}.get(len(moment), "")
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(AS_OF_SQL, (moment, page_id))
        row = cur.fetchone()
    finally:
        conn.close()
    sql = AS_OF_SQL.replace("TIMESTAMP %s", f"TIMESTAMP '{moment}'").replace("page_id = %s", f"page_id = {int(page_id)}")
    if not row:
        return None, "", sql
    return row[0], text(page_id, row[0]), sql


def hunks(page_id, rev_a, rev_b):
    """Paragraphs of version a that are gone in b ("del") and paragraphs new in b ("add")."""
    a = full_text(page_id, rev_a).split("\n\n")
    b = full_text(page_id, rev_b).split("\n\n")
    out = {"del": [], "add": []}
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            out["del"].extend(p for p in a[i1:i2] if p.strip())
        if op in ("replace", "insert"):
            out["add"].extend(p for p in b[j1:j2] if p.strip())
    return out

