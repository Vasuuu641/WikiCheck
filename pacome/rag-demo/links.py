"""B3 Link suggester (/links): articles that are close by meaning but not linked in either direction, and the paragraph where the link fits.

Second view: the articles with the fewest inbound links, each with five places to link from.
Scope: Finnish articles whose links were parsed from the newest wikitext (see paras.py); links from other articles are unknown,
so an inbound count is a count within that set.
"""
from flask import Blueprint, Response, jsonify, request

import rag
import tb

bp = Blueprint("links", __name__)
POOL = 200      # nearest articles fetched from the index; fi is filtered after, because a WHERE lang in the subquery makes MariaDB drop the vector index
SHOW = 10

# 1. Nearest articles (vector index, subquery), kept if Finnish and their links were parsed.
NEIGHBORS_SQL = """\
SELECT a.page_id, a.title, v.distance
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS distance
      FROM article_vector
      ORDER BY distance
      LIMIT ?) AS v
JOIN article a USING (lang, page_id)
WHERE a.lang = 'fi' AND a.page_id <> ? AND a.page_id IN (SELECT from_page_id FROM article_link)
ORDER BY v.distance"""

# 2. In article X, the paragraphs closest to article Y (X has tens of paragraphs: exact distance over its own rows).
WHERE_SQL = """\
SELECT p.para_no, p.heading, p.text, VEC_DISTANCE_COSINE(pv.embedding, av.embedding) AS distance
FROM para_vector pv
JOIN para p USING (page_id, para_no)
JOIN article_vector av ON av.lang = 'fi' AND av.page_id = ?
WHERE pv.page_id = ?
ORDER BY distance
LIMIT 2"""

# 3. Orphan view: fewest inbound links among the articles whose links are known.
ORPHANS_SQL = """\
SELECT a.page_id, a.title, COUNT(l.from_page_id) AS inbound
FROM article a
LEFT JOIN article_link l ON l.to_title = a.title AND l.from_page_id <> a.page_id
WHERE a.lang = 'fi' AND a.page_id IN (SELECT DISTINCT page_id FROM para)
GROUP BY a.page_id, a.title
ORDER BY inbound, a.title
LIMIT ?"""

# 4. Where to link TO an article from: nearest paragraphs (vector index), in articles that do not link to it yet.
PLACES_SQL = """\
SELECT p.page_id, p.para_no, a.title, p.heading, p.text, v.distance
FROM (SELECT page_id, para_no, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS distance
      FROM para_vector
      ORDER BY distance
      LIMIT ?) AS v
JOIN para p USING (page_id, para_no)
JOIN article a ON a.lang = 'fi' AND a.page_id = p.page_id
WHERE p.page_id <> ? AND NOT EXISTS (SELECT 1 FROM article_link l WHERE l.from_page_id = p.page_id AND l.to_title = ?)
ORDER BY v.distance"""


def _vec(cur, page_id):
    cur.execute("SELECT a.title, VEC_ToText(v.embedding) FROM article a JOIN article_vector v USING (lang, page_id) "
                "WHERE lang = 'fi' AND page_id = ?", (page_id,))
    return cur.fetchone()


def _fit(cur, from_pid, to_pid, to_title):
    """The paragraph of article from_pid closest to article to_pid, and whether it already names the target."""
    cur.execute(WHERE_SQL, (to_pid, from_pid))
    return [dict(para_no=n, heading=h, text=t, distance=round(float(d), 4), mentions=to_title.lower() in t.lower())
            for n, h, t, d in cur.fetchall()]


def suggest_for(cur, page_id, hidden=(), pool=POOL, show=SHOW):
    """Close, unlinked neighbours of one article. hidden: link titles (from page_id) to pretend are absent (used by the checks)."""
    title, vec = _vec(cur, page_id)
    cur.execute(f"SET SESSION mhnsw_ef_search = {rag.EF_SEARCH}")
    rows, counters = tb.run_checked(cur, NEIGHBORS_SQL, (vec, pool + 1, page_id))
    cur.execute("SELECT to_title FROM article_link WHERE from_page_id = ?", (page_id,))
    out_links = {t for (t,) in cur.fetchall()} - set(hidden)
    ids = [r[0] for r in rows]
    back = set()
    if ids:
        cur.execute(f"SELECT from_page_id FROM article_link WHERE to_title = ? AND from_page_id IN ({','.join('?' * len(ids))})", [title] + ids)
        back = {p for (p,) in cur.fetchall()}
    items = []
    for pid, t, d in rows:
        if t in out_links or pid in back:
            continue
        items.append(dict(page_id=pid, title=t, distance=round(float(d), 4)))
        if len(items) == show:
            break
    return title, items, counters


def places_for(cur, page_id, title, vec, show=5, pool=POOL):
    """Five articles (one paragraph each) from which a link to this article would fit."""
    cur.execute(f"SET SESSION mhnsw_ef_search = {rag.EF_SEARCH}")
    rows, counters = tb.run_checked(cur, PLACES_SQL, (vec, pool, page_id, title))
    seen, out = set(), []
    for pid, no, t, h, x, d in rows:
        if pid in seen:
            continue
        seen.add(pid)
        out.append(dict(page_id=pid, para_no=no, title=t, heading=h, text=x, distance=round(float(d), 4), mentions=title.lower() in x.lower()))
        if len(out) == show:
            break
    return out, counters


@bp.route("/api/links/articles")
def articles():
    q = request.args.get("q", "").strip()
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT a.page_id, a.title FROM article a WHERE a.lang = 'fi' AND a.title LIKE ? AND a.page_id IN (SELECT DISTINCT page_id FROM para) "
                    "ORDER BY (a.title = ?) DESC, CHAR_LENGTH(a.title) LIMIT 12", (f"%{like}%", q))
        return jsonify(items=[dict(page_id=p, title=t) for p, t in cur.fetchall()])
    finally:
        conn.close()


@bp.route("/api/links/suggest")
def suggest():
    pid = request.args.get("page_id", type=int)
    conn = rag.connect()
    try:
        cur = conn.cursor()
        if not _vec(cur, pid):
            return jsonify(error="article not found"), 404
        title, items, counters = suggest_for(cur, pid)
        for it in items:
            it["place_in_source"] = _fit(cur, pid, it["page_id"], it["title"])      # where in this article to link to the neighbour
            it["place_in_target"] = _fit(cur, it["page_id"], pid, title)            # and where in the neighbour to link back
        return jsonify(title=title, items=items, handler=counters,
                       sql=NEIGHBORS_SQL + "\n\n" + WHERE_SQL)
    finally:
        conn.close()


@bp.route("/api/links/orphans")
def orphans():
    conn = rag.connect()
    try:
        cur = conn.cursor()
        rows, counters = tb.run_checked(cur, ORPHANS_SQL, (30,))
        return jsonify(items=[dict(page_id=p, title=t, inbound=int(n)) for p, t, n in rows], sql=ORPHANS_SQL)
    finally:
        conn.close()


@bp.route("/api/links/places")
def places():
    pid = request.args.get("page_id", type=int)
    conn = rag.connect()
    try:
        cur = conn.cursor()
        row = _vec(cur, pid)
        if not row:
            return jsonify(error="article not found"), 404
        items, counters = places_for(cur, pid, row[0], row[1])
        return jsonify(title=row[0], items=items, handler=counters, sql=PLACES_SQL)
    finally:
        conn.close()


PAGE_BODY = """<h1>Link suggester</h1>
<p class="muted">Finnish articles of the category tree “Suomen sodat”, with their outgoing links read from the newest wikitext.
Links from articles outside this set are unknown, so “inbound” means inbound within the set.</p>
<div class="row"><button id="t1">Close but unlinked</button><button class="alt" id="t2">Fewest inbound links</button></div>
<div id="v1"><div class="row"><input type="text" id="q" placeholder="Article title (e.g. Talvisota)" autocomplete="off"><button id="go">Find</button></div><div id="arts"></div></div>
<div id="v2" hidden></div>
<div id="out"></div>
<details><summary>SQL</summary><div class="sql" id="sql"></div></details>"""

SCRIPT = r"""
const $=s=>document.querySelector(s);const E=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const para=(p,label)=>p?`<p><b>${label}</b> ${p.heading?'· '+E(p.heading):''} ${p.mentions?'<span class="tag ok">names it</span>':''}<br>${E(p.text.slice(0,380))}${p.text.length>380?'…':''}</p>`:'';
function show(n){$('#v1').hidden=n!=1;$('#v2').hidden=n!=2;$('#out').innerHTML='';if(n==2)orphans();}
async function arts(){const r=await (await fetch('/api/links/articles?q='+encodeURIComponent($('#q').value))).json();
 $('#arts').innerHTML=r.items.map(a=>`<div><a href="#" data-p="${a.page_id}">${E(a.title)}</a></div>`).join('')||'<p class="muted">Nothing found.</p>';
 document.querySelectorAll('#arts a').forEach(a=>a.onclick=e=>{e.preventDefault();sug(a.dataset.p)});}
async function sug(p){$('#out').innerHTML='<p class="muted">Searching…</p>';const r=await (await fetch('/api/links/suggest?page_id='+p)).json();
 $('#sql').textContent=r.sql+'\n\n-- plan + Handler_read counters: '+JSON.stringify(r.handler);
 $('#out').innerHTML=`<h2>Close to “${E(r.title)}”, linked in neither direction</h2>`+r.items.map(i=>`<div class="pair"><div class="box"><h4>In “${E(r.title)}”, link to “${E(i.title)}” <span class="tag">distance ${i.distance.toFixed(3)}</span></h4>${para(i.place_in_source[0],'Best paragraph')}</div>
 <div class="box"><h4>In “${E(i.title)}”, link to “${E(r.title)}”</h4>${para(i.place_in_target[0],'Best paragraph')}</div></div>`).join('')||'<p class="muted">No unlinked neighbour.</p>';}
async function orphans(){const r=await (await fetch('/api/links/orphans')).json();$('#sql').textContent=r.sql;
 $('#v2').innerHTML='<h2>Fewest inbound links</h2>'+r.items.map(a=>`<div><a href="#" data-p="${a.page_id}">${E(a.title)}</a> <span class="muted">${a.inbound} inbound</span></div>`).join('');
 document.querySelectorAll('#v2 a').forEach(a=>a.onclick=e=>{e.preventDefault();places(a.dataset.p)});}
async function places(p){$('#out').innerHTML='<p class="muted">Searching…</p>';const r=await (await fetch('/api/links/places?page_id='+p)).json();
 $('#sql').textContent=r.sql+'\n\n-- plan + Handler_read counters: '+JSON.stringify(r.handler);
 $('#out').innerHTML=`<h2>Five places to link to “${E(r.title)}” from</h2>`+r.items.map(i=>`<div class="box" style="margin:8px 0"><h4>${E(i.title)} <span class="tag">distance ${i.distance.toFixed(3)}</span> ${i.mentions?'<span class="tag ok">names it</span>':''}</h4><p>${E(i.text.slice(0,500))}${i.text.length>500?'…':''}</p></div>`).join('');}
$('#t1').onclick=()=>show(1);$('#t2').onclick=()=>show(2);$('#go').onclick=arts;$('#q').onkeydown=e=>{if(e.key=='Enter')arts()};
"""


@bp.route("/links")
def index():
    return Response(tb.page("Link suggester", PAGE_BODY, SCRIPT))
