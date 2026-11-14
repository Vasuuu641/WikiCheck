"""Hub page (/) and the statistics page (/stats). The shared banner and the per-page guide live in guide.py."""
from flask import Blueprint, Response, jsonify

import guide
import rag

bp = Blueprint("hub", __name__)

BANNER = guide.BANNER
NAV = BANNER

STYLE = """<style>:root{--bg:#fff;--fg:#202122;--muted:#54595d;--line:#a2a9b1;--card:#f8f9fa;--link:#3366cc}
@media(prefers-color-scheme:dark){:root{--bg:#101418;--fg:#eaecf0;--muted:#a2a9b1;--line:#54595d;--card:#1b1f23;--link:#88a3e8}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
nav a,a{color:var(--link);text-decoration:none}main{max-width:960px;margin:0 auto;padding:24px 20px 60px}
h1{font:400 2rem Georgia,serif;margin:0 0 6px}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-top:24px}
.card{display:block;border:1px solid var(--line);background:var(--card);padding:16px;color:inherit}.card:hover{border-color:var(--link)}
.card h3{margin:0 0 6px;font:400 1.3rem Georgia,serif;color:var(--link)}
.choose{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-top:28px}.choose .card{padding:22px 24px}.choose h2{margin:0 0 8px;font:400 1.6rem Georgia,serif;color:var(--link)}.choose p{margin:0 0 12px}.choose .list{color:var(--muted);font-size:.9rem}
table{border-collapse:collapse;width:100%;margin:12px 0 28px}td,th{text-align:right;padding:6px 10px;border-bottom:1px solid var(--line)}th:first-child,td:first-child{text-align:left}
.big{display:flex;gap:32px;flex-wrap:wrap;margin:20px 0}.big div b{display:block;font:400 2.4rem Georgia,serif}</style>"""

HUB = NAV + """<main><h1>Wikipedia, History of Finland</h1>
<p class="muted">Finnish, Swedish and English Wikipedia articles stored in MariaDB with their vectors. What did you come to do?</p>
<div class="choose">""" + "".join(
    f'<a class="card" href="{guide.SECTIONS[k][1]}"><h2>{guide.SECTIONS[k][0]}</h2><p>{guide.SECTIONS[k][2]}</p>'
    f'<div class="list">{" · ".join(p[3] for p in guide.BY_SECTION[k])}</div></a>' for k in ("read", "edit")) + "</div></main>"


def _section_page(k):
    name, _, text = guide.SECTIONS[k]
    body = f'{NAV}<main><h1>{name}</h1><p class="muted">{text}</p><div class="cards">{guide.cards(guide.BY_SECTION[k])}</div></main>'
    return Response(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{name}</title>{STYLE}</head><body>{body}</body></html>')


@bp.route("/readers")
def readers():
    return _section_page("read")


@bp.route("/editors")
def editors():
    return _section_page("edit")

STATS_SQL = ("SELECT lang, COUNT(*), SUM(CHAR_LENGTH(text)), MIN(date_modified), MAX(date_modified) FROM article GROUP BY lang")


def _stats():
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute(STATS_SQL)
        langs = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM article_vector")
        vectors = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT category) FROM article_category")
        cats = cur.fetchone()[0]
        pairs = {}
        for a, b in (("fi", "sv"), ("fi", "en"), ("sv", "en")):
            cur.execute("SELECT COUNT(*) FROM article x JOIN article y ON y.wikidata_id = x.wikidata_id "
                        "AND y.lang = ? WHERE x.lang = ?", (b, a))
            pairs[f"{a}-{b}"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM (SELECT wikidata_id FROM article WHERE wikidata_id IS NOT NULL "
                    "GROUP BY wikidata_id HAVING COUNT(*) = 3) t")
        triple = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT wikidata_id) FROM article")
        topics = cur.fetchone()[0]
    finally:
        conn.close()
    return langs, vectors, cats, pairs, triple, topics


NAMES = {"fi": "Finnish", "sv": "Swedish", "en": "English"}


@bp.route("/")
def hub():
    return Response(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Wikipedia · History of Finland</title>{STYLE}</head><body>{HUB}</body></html>')


@bp.route("/stats")
def stats():
    langs, vectors, cats, pairs, triple, topics = _stats()
    total = sum(r[1] for r in langs)
    rows = "".join(f"<tr><td>{NAMES.get(l, l)}</td><td>{n:,}</td><td>{int(c):,}</td><td>{str(lo)[:10]}</td><td>{str(hi)[:10]}</td></tr>"
                   for l, n, c, lo, hi in langs)
    prow = "".join(f"<tr><td>{NAMES[k[:2]]} + {NAMES[k[3:]]}</td><td>{v:,}</td></tr>" for k, v in pairs.items())
    body = f"""{NAV}<main><h1>The numbers</h1><p class="muted">Live counts from MariaDB.</p>
<div class="big"><div><b>{total:,}</b>articles</div><div><b>{vectors:,}</b>vectors (bge-m3, 1024 dims)</div>
<div><b>{topics:,}</b>distinct topics</div><div><b>{cats:,}</b>categories</div></div>
<h2>Articles per language</h2><table><tr><th>Language</th><th>Articles</th><th>Characters</th><th>Oldest edit</th><th>Newest edit</th></tr>{rows}</table>
<h2>Topics that exist in more than one language</h2><table><tr><th>Pair</th><th>Topics</th></tr>{prow}
<tr><td>All three</td><td>{triple:,}</td></tr></table>
<p class="muted">Topics are matched by Wikidata id. Text: Wikipedia, CC BY-SA 4.0.</p></main>"""
    return Response(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Numbers</title>{STYLE}</head><body>{body}</body></html>')
