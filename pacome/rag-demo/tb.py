"""Small helpers shared by the Tier B pages (gaps, refs, links, consistency)."""
import html

import hub
import rag

LANGS = ("fi", "sv", "en")
NAMES = {"fi": "Finnish", "sv": "Swedish", "en": "English"}

CSS = """<style>
:root{--ok:#14866d;--warn:#b32424;--hl:#fef6e7}
@media(prefers-color-scheme:dark){:root{--ok:#2ec4a0;--warn:#ff6b6b;--hl:#2a2418}}
main{max-width:1200px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
input[type=text],select{padding:7px 10px;font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:2px}
input[type=text]{flex:1 1 320px}
button{padding:7px 16px;font:inherit;font-weight:700;color:#fff;background:#36c;border:1px solid #36c;border-radius:2px;cursor:pointer}
button.alt{background:var(--card);color:var(--fg);border-color:var(--line);font-weight:400}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:10px 0 22px}
@media(max-width:800px){.pair{grid-template-columns:1fr}}
.box{border:1px solid var(--line);background:var(--card);padding:10px 12px;font-size:.92rem;overflow-wrap:anywhere}
.box h4{margin:0 0 4px;font:700 .85rem var(--sans,sans-serif)}.box p{margin:4px 0;white-space:pre-wrap}
.tag{display:inline-block;padding:0 7px;border:1px solid var(--line);border-radius:9px;font-size:.78rem;color:var(--muted)}
.tag.hi{border-color:var(--warn);color:var(--warn)}.tag.ok{border-color:var(--ok);color:var(--ok)}
.sql{background:var(--card);border:1px solid var(--line);padding:10px;overflow:auto;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre}
.note{border-left:3px solid var(--warn);background:var(--hl);padding:8px 12px;margin:12px 0}
details{margin:10px 0}summary{cursor:pointer;color:var(--link)}
.muted{color:var(--muted)}
</style>"""


def page(title, body, script=""):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title>{hub.STYLE}{CSS}</head><body>{hub.BANNER}<main>{body}</main>'
            f'<script>{script}</script></body></html>')


def esc(s):
    return html.escape(str(s))


def handler_counters(cur):
    cur.execute("SHOW SESSION STATUS LIKE 'Handler_read%'")
    return {k: int(v) for k, v in cur.fetchall()}


def run_checked(cur, sql, params=()):
    """Execute and return (rows, info). info says how the rows were read:
      handler     SHOW SESSION STATUS LIKE 'Handler_read%' (difference caused by this query)
      plan        EXPLAIN: (table, access type, key) per step
      index_used  True if a VECTOR index is the access path of some table. A table scan shows as type ALL and a large
                  Handler_read_rnd_next; the HNSW index shows as key `embedding`, Handler_read_last 1 and no row scan."""
    cur.execute("EXPLAIN " + sql, params)
    plan = [(r[2], r[3], r[5]) for r in cur.fetchall()]
    before = handler_counters(cur)
    cur.execute(sql, params)
    rows = cur.fetchall()
    after = handler_counters(cur)
    handler = {k: after[k] - before[k] for k in after if after[k] != before[k]}
    return rows, dict(handler=handler, plan=plan, index_used=any(k == "embedding" for _, _, k in plan))


def connect_knn():
    """Connection with the search-time setting the brief asks for."""
    conn = rag.connect()
    conn.cursor().execute(f"SET SESSION mhnsw_ef_search = {rag.EF_SEARCH}")
    return conn


# ---------------------------------------------------------------- cached yes/no checks on a pair of passages (B2, B4)
import hashlib

import decisions

MAX_PASSAGE = 1500

SAME_FACT = ("Passage B is: \"{b}\"\n\nThe text is passage A. Does passage A state the same specific fact as passage B (the same event, "
             "person, date or figure), and not merely the same general topic? Judge only what the passages say, not what is true.")
SAME_SUBJECT = ("Passage B is: \"{b}\"\n\nThe text is passage A. Are A and B about the very same specific subject: the same person, place, "
                "building, organisation, event or election? Two different instances of the same kind of thing (two different people, two different "
                "elections, two factories, two churches, two parishes, two relatives with the same surname) are NOT the same subject. Each passage starts "
                "with the title of its Wikipedia article and the section heading: use them. Judge only what the passages say.")
INCOMPATIBLE = ("Passage B is: \"{b}\"\n\nThe text is passage A. Considering only facts that both passages state about the same subject, "
                "do A and B give INCOMPATIBLE values (different dates, numbers, names or outcomes), so that both cannot be true? "
                "Answer yes only if both state the same fact and the values contradict. A different wording, a different level of detail, "
                "a rounded figure, or a fact only one of them mentions is not incompatible. Each passage starts with the title of its Wikipedia article "
                "and the section heading. Judge only what the passages say, not what is true.")
_ready = False


def _ensure(cur):
    global _ready
    if not _ready:
        cur.execute("CREATE TABLE IF NOT EXISTS pair_check (kind VARCHAR(16) NOT NULL, sha CHAR(40) NOT NULL, p_yes FLOAT NOT NULL, "
                    "created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (kind, sha))")
        _ready = True


def pair_check_multi(specs, pairs):
    """specs: {kind: template}; pairs: [(text_a, text_b)] -> [{kind: p_yes}]. One request per pair asks every kind that is not cached yet."""
    conn = rag.connect()
    conn.autocommit = True
    try:
        cur = conn.cursor()
        _ensure(cur)
        keys = [{k: hashlib.sha1(f"{tpl}|{a[:MAX_PASSAGE]}|{b[:MAX_PASSAGE]}".encode()).hexdigest() for k, tpl in specs.items()} for a, b in pairs]
        have = {}
        allkeys = [s for ks in keys for s in ks.values()]
        for i in range(0, len(allkeys), 400):
            part = allkeys[i:i + 400]
            cur.execute(f"SELECT kind, sha, p_yes FROM pair_check WHERE sha IN ({','.join('?' * len(part))})", part)
            have.update({(k, s): float(p) for k, s, p in cur.fetchall()})
        jobs, owners = [], []
        for i, ((a, b), ks) in enumerate(zip(pairs, keys)):
            missing = {k: specs[k] for k, s in ks.items() if (k, s) not in have}
            if missing:
                owners.append((i, list(missing)))
                jobs.append((a[:MAX_PASSAGE], {k: {"type": "noul", "instructions": tpl.format(b=b[:MAX_PASSAGE])} for k, tpl in missing.items()}))
        rows = []
        for (i, kinds), ans in zip(owners, decisions.ask_many(jobs)):
            for k in kinds:
                have[(k, keys[i][k])] = float(ans[k]["noul"])
                rows.append((k, keys[i][k], have[(k, keys[i][k])]))
        if rows:
            cur.executemany("INSERT IGNORE INTO pair_check (kind, sha, p_yes) VALUES (?,?,?)", rows)
        return [{k: have[(k, s)] for k, s in ks.items()} for ks in keys]
    finally:
        conn.close()


def pair_check(kind, template, pairs):
    """pairs: [(text_a, text_b)] -> [p_yes] for one yes/no question, cached in pair_check."""
    return [r[kind] for r in pair_check_multi({kind: template}, pairs)]


def mostly_table(text):
    """Markdown tables of numbers and names say nothing checkable in prose: more than 40% of the lines are table rows."""
    lines = [l for l in text.split("\n") if l.strip()]
    return bool(lines) and sum(1 for l in lines if l.lstrip().startswith("|")) / len(lines) > 0.4
