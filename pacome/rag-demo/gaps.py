"""B1 Cross-language gap finder (/gaps): which sections of the sv/en article have no counterpart in the fi article, and the reverse.

For every chunk of the source article, the nearest chunk inside the destination article of the same topic (same wikidata_id).
bge-m3 is multilingual, so nothing is translated. A large cosine distance means the destination has no such section.
No LLM is involved.
"""
from flask import Blueprint, jsonify, request

import chunks
import rag
import tb

bp = Blueprint("gaps", __name__)

# Both articles are small (tens of chunks), so the nearest chunk inside one article is an exact distance over its own rows.
# (The HNSW index answers "nearest in the whole table"; it cannot be pointed at one article.)
PAIR_SQL = """\
SELECT s.chunk_no, t.chunk_no, VEC_DISTANCE_COSINE(s.embedding, t.embedding) AS distance
FROM chunk_vector s
JOIN chunk_vector t ON t.lang = ? AND t.page_id = ?
WHERE s.lang = ? AND s.page_id = ?"""

CHUNKS_SQL = "SELECT chunk_no, heading, text FROM chunk WHERE lang = ? AND page_id = ? ORDER BY chunk_no"

TOPIC_SQL = """\
SELECT a.wikidata_id, a.lang, a.title, a.page_id
FROM article a
WHERE a.wikidata_id IN (SELECT wikidata_id FROM (SELECT wikidata_id FROM article WHERE title LIKE ? AND wikidata_id IS NOT NULL
                                                  ORDER BY (title = ?) DESC, CHAR_LENGTH(title) LIMIT 12) x)
ORDER BY a.wikidata_id, a.lang"""

# Two levels, chosen on 64 hand-labelled sections (docs/tier-b-checks.md): above HIGH a section is very probably missing
# (6 of 7 labelled sections were), between LOW and HIGH it is only worth a look (about one in three was missing).
HIGH = 0.40
LOW = 0.32
THRESHOLD = HIGH


def topic_articles(cur, wikidata_id):
    cur.execute("SELECT lang, page_id, title FROM article WHERE wikidata_id = ?", (wikidata_id,))
    return {l: dict(page_id=p, title=t) for l, p, t in cur.fetchall()}


def gaps(cur, src_lang, src_pid, dst_lang, dst_pid):
    """Source chunks ranked by distance to their nearest chunk in the destination article (largest first)."""
    cur.execute(CHUNKS_SQL, (src_lang, src_pid))
    src = {n: (h, t) for n, h, t in cur.fetchall() if not chunks.is_reference_list(h, t)}
    cur.execute(CHUNKS_SQL, (dst_lang, dst_pid))
    dst = {n: (h, t) for n, h, t in cur.fetchall() if not chunks.is_reference_list(h, t)}
    rows, info = tb.run_checked(cur, PAIR_SQL, (dst_lang, dst_pid, src_lang, src_pid))
    best = {}
    for s_no, t_no, d in rows:
        if s_no not in src or t_no not in dst:      # reference lists are not content
            continue
        d = float(d)
        if s_no not in best or d < best[s_no][1]:
            best[s_no] = (t_no, d)
    out = [dict(chunk_no=s_no, heading=src[s_no][0], text=src[s_no][1], distance=round(d, 4),
                nearest_no=t_no, nearest_heading=dst[t_no][0], nearest_text=dst[t_no][1]) for s_no, (t_no, d) in best.items()]
    out.sort(key=lambda r: -r["distance"])
    return out, info


@bp.route("/api/gaps/topics")
def topics():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify(items=[])
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute(TOPIC_SQL, (f"%{like}%", q))
        items = {}
        for wid, lang, title, pid in cur.fetchall():
            items.setdefault(wid, dict(wikidata_id=wid, titles={}))["titles"][lang] = title
        return jsonify(items=[i for i in items.values() if len(i["titles"]) > 1])
    finally:
        conn.close()


@bp.route("/api/gaps")
def api():
    wid, src, dst = request.args.get("wikidata_id", ""), request.args.get("src", ""), request.args.get("dst", "")
    if src not in tb.LANGS or dst not in tb.LANGS or src == dst:
        return jsonify(error="bad language pair"), 400
    conn = rag.connect()
    try:
        cur = conn.cursor()
        arts = topic_articles(cur, wid)
        if src not in arts or dst not in arts:
            return jsonify(error=f"this topic has no {src if src not in arts else dst} article"), 404
        rows, counters = gaps(cur, src, arts[src]["page_id"], dst, arts[dst]["page_id"])
        return jsonify(src=dict(lang=src, **arts[src]), dst=dict(lang=dst, **arts[dst]), high=HIGH, low=LOW, items=rows,
                       handler=counters, sql=PAIR_SQL, available=sorted(arts))
    finally:
        conn.close()


PAGE_BODY = """<h1>Cross-language gap finder</h1>
<p class="muted">Pick a topic that exists in at least two languages. Each section of one article is compared with the sections of the
same topic's article in another language; sections whose nearest match is far away are probably missing there. Both directions are shown.</p>
<div class="row"><input type="text" id="q" placeholder="Topic, in any language (e.g. Talvisota, Åland, Mannerheim)" autocomplete="off">
<button id="go">Find topics</button></div>
<div id="topics"></div>
<div class="row" id="pairs" hidden><span class="muted" style="align-self:center">Compare</span><select id="src"></select><span style="align-self:center">→</span><select id="dst"></select>
<button id="run">Show missing sections</button></div>
<div id="out"></div>
<details><summary>SQL</summary><div class="sql" id="sql"></div></details>"""

SCRIPT = r"""
const $=s=>document.querySelector(s);const E=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const N={fi:'Finnish',sv:'Swedish',en:'English'};let cur=null;
async function topics(){const r=await (await fetch('/api/gaps/topics?q='+encodeURIComponent($('#q').value))).json();
 $('#topics').innerHTML=r.items.map(t=>`<div><a href="#" data-w="${t.wikidata_id}" data-l="${Object.keys(t.titles).join(',')}">${Object.entries(t.titles).map(([l,x])=>l+': '+E(x)).join(' · ')}</a></div>`).join('')||'<p class="muted">No topic found in two languages.</p>';
 document.querySelectorAll('#topics a').forEach(a=>a.onclick=e=>{e.preventDefault();pick(a.dataset.w,a.dataset.l.split(','))});}
function pick(w,langs){cur=w;const o=langs.map(l=>`<option value="${l}">${N[l]}</option>`).join('');$('#src').innerHTML=o;$('#dst').innerHTML=o;
 $('#src').value=langs.includes('sv')?'sv':langs[0];$('#dst').value=langs.includes('fi')&&$('#src').value!='fi'?'fi':langs.find(l=>l!=$('#src').value);$('#pairs').hidden=false;run();}
async function run(){if(!cur)return;$('#out').innerHTML='<p class="muted">Searching…</p>';
 const r=await (await fetch(`/api/gaps?wikidata_id=${cur}&src=${$('#src').value}&dst=${$('#dst').value}`)).json();
 if(r.error){$('#out').innerHTML='<p class="note">'+E(r.error)+'</p>';return}
 $('#sql').textContent=r.sql+'\n\n-- plan + Handler_read counters: '+JSON.stringify(r.handler);
 const miss=r.items.filter(i=>i.distance>r.high), maybe=r.items.filter(i=>i.distance>r.low&&i.distance<=r.high);
 let h=`<h2>${N[r.src.lang]} “${E(r.src.title)}” sections probably missing in ${N[r.dst.lang]} “${E(r.dst.title)}”</h2><p class="muted">${miss.length} of ${r.items.length} sections have no match closer than ${r.high}. ${maybe.length} more are worth a look (between ${r.low} and ${r.high}; about one in three of those is really missing).</p>`;
 const card=i=>`<div class="pair"><div class="box"><h4>${E(r.src.lang)} · ${E(i.heading||'(lead)')} <span class="tag ${i.distance>r.high?'hi':i.distance>r.low?'':'ok'}">distance ${i.distance.toFixed(3)}</span></h4><p>${E(i.text.slice(0,900))}${i.text.length>900?'…':''}</p></div>
 <div class="box"><h4>Nearest in ${E(r.dst.lang)} · ${E(i.nearest_heading||'(lead)')}</h4><p>${E(i.nearest_text.slice(0,500))}${i.nearest_text.length>500?'…':''}</p></div></div>`;
 h+=miss.map(card).join('');if(maybe.length)h+=`<details><summary>${maybe.length} sections that may be missing</summary>${maybe.map(card).join('')}</details>`;
 const rest=r.items.filter(i=>i.distance<=r.low);
 if(rest.length)h+=`<details><summary>${rest.length} sections that do have a counterpart</summary>${rest.map(card).join('')}</details>`;
 $('#out').innerHTML=h;}
$('#go').onclick=topics;$('#q').onkeydown=e=>{if(e.key=='Enter')topics()};$('#run').onclick=run;
"""


@bp.route("/gaps")
def index():
    from flask import Response
    return Response(tb.page("Cross-language gap finder", PAGE_BODY, SCRIPT))
