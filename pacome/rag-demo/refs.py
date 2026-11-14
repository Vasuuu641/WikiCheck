"""B2 Reference borrower (/refs): for a Finnish paragraph without a reference, find paragraphs in OTHER articles that say the same
thing and do have one, confirm with a yes/no check, and offer their reference.

Wikipedia cannot cite itself, so plain RAG does not give a source; this borrows the source another article already uses.
The editor still has to open the source and check that it supports the sentence.
"""
from flask import Blueprint, Response, jsonify, request

import rag
import tb

bp = Blueprint("refs", __name__)
POOL = 40          # nearest paragraphs fetched from the index
SHOW = 5           # candidates (with a ref) that are checked and shown
CONFIRM = 0.5      # P(same fact) from the check above which a candidate counts as confirmed

# Vector search in a subquery on the vector table, join afterwards; has_ref is filtered after the index has answered.
KNN_SQL = """\
SELECT p.page_id, p.para_no, a.title, p.heading, p.text, v.distance
FROM (SELECT page_id, para_no, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS distance
      FROM para_vector
      ORDER BY distance
      LIMIT ?) AS v
JOIN para p USING (page_id, para_no)
JOIN article a ON a.lang = 'fi' AND a.page_id = p.page_id
WHERE p.has_ref AND p.page_id <> ?
ORDER BY v.distance
LIMIT ?"""

VEC_SQL = "SELECT VEC_ToText(embedding) FROM para_vector WHERE page_id = ? AND para_no = ?"
REFS_SQL = "SELECT page_id, para_no, ref_text FROM para_ref WHERE (page_id, para_no) IN ({marks}) ORDER BY page_id, para_no, ref_no"


def candidates(cur, page_id, para_no, vec=None, pool=POOL, show=SHOW):
    """(rows, handler counters): paragraphs with a ref, other articles, nearest first. Each row has its refs."""
    if vec is None:
        cur.execute(VEC_SQL, (page_id, para_no))
        vec = cur.fetchone()[0]
    cur.execute(f"SET SESSION mhnsw_ef_search = {rag.EF_SEARCH}")
    rows, counters = tb.run_checked(cur, KNN_SQL, (vec, pool, page_id, show))
    out = [dict(page_id=p, para_no=n, title=t, heading=h, text=x, distance=round(float(d), 4), refs=[]) for p, n, t, h, x, d in rows]
    if out:
        marks = ",".join(["(?,?)"] * len(out))
        cur.execute(REFS_SQL.format(marks=marks), [v for r in out for v in (r["page_id"], r["para_no"])])
        by = {}
        for p, n, txt in cur.fetchall():
            by.setdefault((p, n), []).append(txt)
        for r in out:
            r["refs"] = by.get((r["page_id"], r["para_no"]), [])
    return out, counters


def confirmed(text, cands):
    """Add p_same to each candidate with the cached yes/no check."""
    ps = tb.pair_check("same_fact", tb.SAME_FACT, [(c["text"], text) for c in cands])
    for c, p in zip(cands, ps):
        c["p_same"] = round(p, 3)
    return cands


@bp.route("/api/refs/articles")
def articles():
    q = request.args.get("q", "").strip()
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT a.page_id, a.title, SUM(NOT p.has_ref) AS unsourced, COUNT(*) FROM para p JOIN article a ON a.lang='fi' AND a.page_id = p.page_id "
                    "WHERE a.title LIKE ? GROUP BY a.page_id, a.title HAVING unsourced > 0 ORDER BY unsourced DESC LIMIT 15", (f"%{like}%",))
        return jsonify(items=[dict(page_id=p, title=t, unsourced=int(u), paragraphs=n) for p, t, u, n in cur.fetchall()])
    finally:
        conn.close()


@bp.route("/api/refs/paras")
def paras():
    pid = request.args.get("page_id", type=int)
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT para_no, heading, text FROM para WHERE page_id = ? AND NOT has_ref ORDER BY para_no", (pid,))
        return jsonify(items=[dict(para_no=n, heading=h, text=t) for n, h, t in cur.fetchall()])
    finally:
        conn.close()


@bp.route("/api/refs/suggest")
def suggest():
    pid, no = request.args.get("page_id", type=int), request.args.get("para_no", type=int)
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT text FROM para WHERE page_id = ? AND para_no = ?", (pid, no))
        row = cur.fetchone()
        if not row:
            return jsonify(error="paragraph not found"), 404
        cands, counters = candidates(cur, pid, no)
    finally:
        conn.close()
    confirmed(row[0], cands)
    return jsonify(text=row[0], items=cands, confirm=CONFIRM, handler=counters, sql=KNN_SQL)


PAGE_BODY = """<h1>Reference borrower</h1>
<p class="muted">Finnish paragraphs that have no reference. For one of them, MariaDB finds the nearest paragraphs in other articles that
<em>do</em> have a reference, a yes/no check confirms that they state the same fact, and the reference they use is shown.</p>
<div class="note"><b>This is a lead, not a citation.</b> The editor still has to open the source and check that it really supports the sentence.
Scope for now: the Finnish articles of the category tree “Suomen sodat”.</div>
<div class="row"><input type="text" id="q" placeholder="Article title (e.g. Talvisota, Jatkosota, Lapin sota)" autocomplete="off"><button id="go">Find</button></div>
<div id="arts"></div><div id="paras"></div><div id="out"></div>
<details><summary>SQL</summary><div class="sql" id="sql"></div></details>"""

SCRIPT = r"""
const $=s=>document.querySelector(s);const E=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));let pid=null;
async function arts(){const r=await (await fetch('/api/refs/articles?q='+encodeURIComponent($('#q').value))).json();
 $('#arts').innerHTML=r.items.map(a=>`<div><a href="#" data-p="${a.page_id}">${E(a.title)}</a> <span class="muted">${a.unsourced} of ${a.paragraphs} paragraphs without a reference</span></div>`).join('')||'<p class="muted">Nothing found.</p>';
 document.querySelectorAll('#arts a').forEach(a=>a.onclick=e=>{e.preventDefault();open_(a.dataset.p)});}
async function open_(p){pid=p;$('#out').innerHTML='';const r=await (await fetch('/api/refs/paras?page_id='+p)).json();
 $('#paras').innerHTML='<h2>Paragraphs without a reference</h2>'+r.items.map(i=>`<div class="box" style="margin:6px 0"><p>${E(i.text.slice(0,420))}${i.text.length>420?'…':''}</p><button class="alt" data-n="${i.para_no}">Find a reference</button></div>`).join('');
 document.querySelectorAll('#paras button').forEach(b=>b.onclick=()=>suggest(b.dataset.n));}
async function suggest(n){$('#out').innerHTML='<p class="muted">Searching and checking…</p>';
 const r=await (await fetch(`/api/refs/suggest?page_id=${pid}&para_no=${n}`)).json();if(r.error){$('#out').innerHTML=E(r.error);return}
 $('#sql').textContent=r.sql+'\n\n-- plan + Handler_read counters: '+JSON.stringify(r.handler);
 let h='<h2>Candidates</h2><div class="box"><h4>Paragraph without reference</h4><p>'+E(r.text)+'</p></div>';
 h+=r.items.map(i=>`<div class="pair"><div class="box"><h4>${E(i.title)}${i.heading?' · '+E(i.heading):''} <span class="tag">distance ${i.distance.toFixed(3)}</span> <span class="tag ${i.p_same>=r.confirm?'ok':''}">${i.p_same>=r.confirm?'same fact':'not confirmed'} (${i.p_same.toFixed(2)})</span></h4><p>${E(i.text)}</p></div>
 <div class="box"><h4>Reference used there</h4>${i.refs.map(x=>'<p>'+E(x)+'</p>').join('')}</div></div>`).join('')||'<p class="muted">No paragraph with a reference is close enough.</p>';
 $('#out').innerHTML=h;}
$('#go').onclick=arts;$('#q').onkeydown=e=>{if(e.key=='Enter')arts()};
"""


@bp.route("/refs")
def index():
    return Response(tb.page("Reference borrower", PAGE_BODY, SCRIPT))
