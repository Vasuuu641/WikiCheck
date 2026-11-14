"""B4 Consistency checker (/consistency): for one section of an article, the closest sections in OTHER articles of the same language,
and a yes/no check whether the two give incompatible values (dates, figures, names) for the same fact.

Findings are cached (pair_check keeps every answer, consistency_finding keeps the flagged pairs). The page shows both passages
side by side and never says which one is right: that is for the editor to settle with sources.
"""
from flask import Blueprint, Response, jsonify, request

import rag
import tb

bp = Blueprint("consistency", __name__)
POOL = 150           # nearest chunks fetched from the index (all languages); the language is filtered after, so the index stays in use
SHOW = 6             # neighbours checked per chunk
MAX_DISTANCE = 0.35  # neighbours farther than this are not about the same fact (chosen on the labelled sample, docs/tier-b-checks.md)
FLAG = 0.5           # P(same subject and incompatible) at or above which a pair is reported as 'may differ'
LOW = 0.3            # between LOW and FLAG: 'worth a look' (see docs/tier-b-checks.md for how often each level was right)

KNN_SQL = """\
SELECT c.page_id, c.chunk_no, a.title, c.heading, c.text, v.distance
FROM (SELECT lang, page_id, chunk_no, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS distance
      FROM chunk_vector
      ORDER BY distance
      LIMIT ?) AS v
JOIN chunk c USING (lang, page_id, chunk_no)
JOIN article a USING (lang, page_id)
WHERE c.lang = ? AND c.page_id <> ? AND v.distance <= ?
ORDER BY v.distance
LIMIT ?"""

_ready = False


def _ensure(cur):
    global _ready
    if not _ready:
        cur.execute("CREATE TABLE IF NOT EXISTS consistency_finding (lang CHAR(2) NOT NULL, page_a INT UNSIGNED NOT NULL, chunk_a SMALLINT UNSIGNED NOT NULL, "
                    "page_b INT UNSIGNED NOT NULL, chunk_b SMALLINT UNSIGNED NOT NULL, distance FLOAT NOT NULL, p_incompatible FLOAT NOT NULL, "
                    "created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (lang, page_a, chunk_a, page_b, chunk_b)) CHARACTER SET utf8mb4")
        _ready = True


def _labelled(c):
    """The passage as the yes/no check sees it: article title and section heading first (two people with one surname are two subjects)."""
    return f"Article: {c['title']}" + (f" / section: {c['heading']}" if c["heading"] else "") + "\n" + c["text"]


def check_chunk(cur, lang, page_id, chunk_no, show=SHOW, max_distance=MAX_DISTANCE):
    """-> (source chunk dict, [neighbour dicts with p_incompatible], handler counters)."""
    cur.execute("SELECT a.title, c.heading, c.text, VEC_ToText(v.embedding) FROM chunk c JOIN chunk_vector v USING (lang, page_id, chunk_no) "
                "JOIN article a USING (lang, page_id) WHERE lang = ? AND page_id = ? AND chunk_no = ?", (lang, page_id, chunk_no))
    row = cur.fetchone()
    if not row:
        return None, [], {}
    src = dict(page_id=page_id, chunk_no=chunk_no, title=row[0], heading=row[1], text=row[2])
    if tb.mostly_table(src["text"]):
        return src, [], {}
    cur.execute(f"SET SESSION mhnsw_ef_search = {rag.EF_SEARCH}")
    rows, counters = tb.run_checked(cur, KNN_SQL, (row[3], POOL, lang, page_id, max_distance, show))
    items = [dict(page_id=p, chunk_no=n, title=t, heading=h, text=x, distance=round(float(d), 4)) for p, n, t, h, x, d in rows
             if not tb.mostly_table(x)]
    if items:
        both = tb.pair_check_multi({"same_subject": tb.SAME_SUBJECT, "incompatible": tb.INCOMPATIBLE}, [(_labelled(src), _labelled(it)) for it in items])
        _ensure(cur)
        for it, p in zip(items, both):
            it["p_same_subject"] = round(p["same_subject"], 3)
            it["p_incompatible"] = round(p["same_subject"] * p["incompatible"], 3)      # both must hold
        cur.executemany("REPLACE INTO consistency_finding (lang, page_a, chunk_a, page_b, chunk_b, distance, p_incompatible) VALUES (?,?,?,?,?,?,?)",
                        [(lang, page_id, chunk_no, it["page_id"], it["chunk_no"], it["distance"], it["p_incompatible"]) for it in items])
        cur.connection.commit()
    return src, items, counters


@bp.route("/api/consistency/articles")
def articles():
    q = request.args.get("q", "").strip()
    lang = request.args.get("lang", "fi")
    if lang not in tb.LANGS:
        return jsonify(error="bad language"), 400
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT page_id, title FROM article WHERE lang = ? AND title LIKE ? AND page_id IN (SELECT DISTINCT page_id FROM chunk WHERE lang = ?) "
                    "ORDER BY (title = ?) DESC, CHAR_LENGTH(title) LIMIT 12", (lang, f"%{like}%", lang, q))
        return jsonify(items=[dict(page_id=p, title=t) for p, t in cur.fetchall()])
    finally:
        conn.close()


@bp.route("/api/consistency/chunks")
def chunk_list():
    lang, pid = request.args.get("lang", ""), request.args.get("page_id", type=int)
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT chunk_no, heading, LEFT(text, 160) FROM chunk WHERE lang = ? AND page_id = ? AND CHAR_LENGTH(text) >= 150 ORDER BY chunk_no", (lang, pid))
        return jsonify(items=[dict(chunk_no=n, heading=h, snippet=s) for n, h, s in cur.fetchall()])
    finally:
        conn.close()


@bp.route("/api/consistency/check")
def check():
    lang, pid, no = request.args.get("lang", ""), request.args.get("page_id", type=int), request.args.get("chunk_no", type=int)
    if lang not in tb.LANGS:
        return jsonify(error="bad language"), 400
    conn = rag.connect()
    try:
        cur = conn.cursor()
        src, items, counters = check_chunk(cur, lang, pid, no)
        if not src:
            return jsonify(error="chunk not found"), 404
        return jsonify(source=src, items=items, flag=FLAG, low=LOW, max_distance=MAX_DISTANCE, handler=counters, sql=KNN_SQL)
    finally:
        conn.close()


@bp.route("/api/consistency/findings")
def findings():
    conn = rag.connect()
    try:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute("SELECT f.lang, f.page_a, f.chunk_a, a.title, b.title, f.p_incompatible FROM consistency_finding f "
                    "JOIN article a ON a.lang = f.lang AND a.page_id = f.page_a JOIN article b ON b.lang = f.lang AND b.page_id = f.page_b "
                    "WHERE f.p_incompatible >= ? ORDER BY f.p_incompatible DESC LIMIT 40", (LOW,))
        return jsonify(items=[dict(lang=l, page_id=p, chunk_no=n, a=a, b=b, p=round(float(x), 3)) for l, p, n, a, b, x in cur.fetchall()])
    finally:
        conn.close()


PAGE_BODY = """<h1>Consistency checker</h1>
<p class="muted">Pick a section. MariaDB finds the closest sections in <em>other</em> articles of the same language, and a yes/no check asks whether
the two give incompatible values (dates, figures, names) for the same fact. Answers are cached.</p>
<div class="note"><b>Both passages are shown as written, and this page never says which one is right.</b> A flag means “look at this”; the editor settles it with sources.</div>
<div class="row"><select id="lang"><option value="fi">Finnish</option><option value="sv">Swedish</option><option value="en">English</option></select>
<input type="text" id="q" placeholder="Article title (e.g. Talvisota)" autocomplete="off"><button id="go">Find</button></div>
<div id="arts"></div><div id="chunks"></div><div id="out"></div>
<details><summary>Flagged pairs found so far</summary><div id="found"></div></details>
<details><summary>SQL</summary><div class="sql" id="sql"></div></details>"""

SCRIPT = r"""
const $=s=>document.querySelector(s);const E=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));let pid=null;
async function arts(){const r=await (await fetch(`/api/consistency/articles?lang=${$('#lang').value}&q=`+encodeURIComponent($('#q').value))).json();
 $('#arts').innerHTML=r.items.map(a=>`<div><a href="#" data-p="${a.page_id}">${E(a.title)}</a></div>`).join('')||'<p class="muted">Nothing found.</p>';
 document.querySelectorAll('#arts a').forEach(a=>a.onclick=e=>{e.preventDefault();open_(a.dataset.p)});}
async function open_(p){pid=p;$('#out').innerHTML='';const r=await (await fetch(`/api/consistency/chunks?lang=${$('#lang').value}&page_id=${p}`)).json();
 $('#chunks').innerHTML='<h2>Sections</h2>'+r.items.map(i=>`<div class="box" style="margin:6px 0"><b>${E(i.heading||'(lead)')}</b><p>${E(i.snippet)}…</p><button class="alt" data-n="${i.chunk_no}">Check this section</button></div>`).join('');
 document.querySelectorAll('#chunks button').forEach(b=>b.onclick=()=>run(b.dataset.n));}
async function run(n){$('#out').innerHTML='<p class="muted">Searching and checking…</p>';
 const r=await (await fetch(`/api/consistency/check?lang=${$('#lang').value}&page_id=${pid}&chunk_no=${n}`)).json();if(r.error){$('#out').innerHTML=E(r.error);return}
 $('#sql').textContent=r.sql+'\n\n-- plan + Handler_read counters: '+JSON.stringify(r.handler);
 const flagged=r.items.filter(i=>i.p_incompatible>=r.low);
 $('#out').innerHTML=`<h2>${flagged.length} of ${r.items.length} close passages may give different values</h2><p class="muted">“May differ” is right about as often as not, and most real differences score lower: treat both levels as leads.</p>`+r.items.map(i=>`<div class="pair"><div class="box"><h4>${E(r.source.title)} · ${E(r.source.heading||'(lead)')}</h4><p>${E(r.source.text.slice(0,900))}</p></div>
 <div class="box"><h4>${E(i.title)} · ${E(i.heading||'(lead)')} <span class="tag">distance ${i.distance.toFixed(3)}</span> <span class="tag ${i.p_incompatible>=r.flag?'hi':''}">${i.p_incompatible>=r.flag?'may differ':i.p_incompatible>=r.low?'worth a look':'no conflict found'} (${i.p_incompatible.toFixed(2)})</span></h4><p>${E(i.text.slice(0,900))}</p></div></div>`).join('');
 found();}
async function found(){const r=await (await fetch('/api/consistency/findings')).json();$('#found').innerHTML=r.items.map(i=>`<div>${E(i.a)} ↔ ${E(i.b)} <span class="muted">${i.p}</span></div>`).join('')||'<p class="muted">None yet.</p>';}
$('#go').onclick=arts;$('#q').onkeydown=e=>{if(e.key=='Enter')arts()};found();
"""


@bp.route("/consistency")
def index():
    return Response(tb.page("Consistency checker", PAGE_BODY, SCRIPT))
