"""Language mirror: how far apart do fi / sv / en Wikipedia tell the same topic?

Topic = same wikidata_id. Gap = cosine distance between the two stored bge-m3 vectors, computed by
one SQL statement in MariaDB. Map positions come from a one-off PCA of all vectors (`python mirror.py build`),
stored in the table mirror_point. Routes are registered by app.py (Blueprint `mirror`).
"""
import json
import os
import re
import time

import mariadb
import requests
from flask import Blueprint, Response, jsonify, request

import hub
import rag

bp = Blueprint("mirror", __name__)
LANGS = ("fi", "sv", "en")
NAMES = {"fi": "Finnish", "sv": "Swedish", "en": "English"}

PAIRS_SQL = """\
SELECT a.wikidata_id, a.title, b.title, a.page_id, b.page_id,
       VEC_DISTANCE_COSINE(va.embedding, vb.embedding) AS gap,
       pa.x, pa.y, pb.x, pb.y
FROM article a
JOIN article b ON b.wikidata_id = a.wikidata_id AND b.lang = ? AND a.lang = ?
JOIN article_vector va ON va.lang = a.lang AND va.page_id = a.page_id
JOIN article_vector vb ON vb.lang = b.lang AND vb.page_id = b.page_id
JOIN mirror_point pa ON pa.lang = a.lang AND pa.page_id = a.page_id
JOIN mirror_point pb ON pb.lang = b.lang AND pb.page_id = b.page_id
ORDER BY gap DESC"""


def build():
    """One-off: project every stored vector to 2D (PCA, numpy) into table mirror_point."""
    import numpy as np
    conn = rag.connect()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS mirror_point (lang CHAR(2) NOT NULL, page_id INT UNSIGNED NOT NULL, "
                "x FLOAT NOT NULL, y FLOAT NOT NULL, PRIMARY KEY (lang, page_id))")
    cur.execute("SELECT lang, page_id, VEC_ToText(embedding) FROM article_vector")
    keys, rows = [], []
    for lang, pid, txt in cur:
        keys.append((lang, pid))
        rows.append(json.loads(txt))
    m = np.asarray(rows, dtype=np.float32)
    m -= m.mean(axis=0)
    _, _, vt = np.linalg.svd(m, full_matrices=False)
    xy = m @ vt[:2].T
    xy /= np.abs(xy).max()
    cur.execute("TRUNCATE mirror_point")
    cur.executemany("INSERT INTO mirror_point VALUES (?,?,?,?)",
                    [(l, p, float(x), float(y)) for (l, p), (x, y) in zip(keys, xy)])
    conn.commit()
    print(f"mirror_point: {len(keys)} points")


@bp.route("/api/mirror/pairs")
def pairs():
    a, b = request.args.get("a", "fi"), request.args.get("b", "en")
    if a not in LANGS or b not in LANGS or a == b:
        return jsonify(error="pick two different languages from fi, sv, en"), 400
    t0 = time.perf_counter()
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute(PAIRS_SQL, (b, a))
        rows = cur.fetchall()
    finally:
        conn.close()
    ms = (time.perf_counter() - t0) * 1000
    items = [dict(wid=r[0], ta=r[1], tb=r[2], pa=r[3], pb=r[4], gap=round(float(r[5]), 4),
                  ax=round(r[6], 4), ay=round(r[7], 4), bx=round(r[8], 4), by=round(r[9], 4)) for r in rows]
    return jsonify(a=a, b=b, ms=ms, sql=PAIRS_SQL, items=items)


def _article(wid, lang):
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT title, url, text FROM article WHERE wikidata_id = ? AND lang = ?", (wid, lang))
        return cur.fetchone()
    finally:
        conn.close()


NOTE_SYSTEM = (
    "You compare two Wikipedia articles about the same topic written in different languages, using ONLY "
    "the two texts given. Output exactly two bullet lists with these headings on their own line: "
    "'Only in {A}' and 'Only in {B}', 3 bullets max each (write 'nothing notable' if empty). "
    "Every bullet must end with a citation in the form [A: \"short verbatim quote\"] or [B: \"short verbatim quote\"] "
    "copied from the text that contains the fact. For a bullet about something missing from the other article, "
    "quote the article that has it. Write in English, plain text, max 150 words total."
)


@bp.route("/api/mirror/compare")
def compare():
    wid, a, b = request.args.get("wid", ""), request.args.get("a", ""), request.args.get("b", "")
    if a not in LANGS or b not in LANGS or not re.fullmatch(r"Q\d+", wid):
        return jsonify(error="bad request"), 400
    ra, rb = _article(wid, a), _article(wid, b)
    if not ra or not rb:
        return jsonify(error="article not found"), 404
    return jsonify(a=dict(lang=a, title=ra[0], url=ra[1], text=ra[2][:4000], chars=len(ra[2])),
                   b=dict(lang=b, title=rb[0], url=rb[1], text=rb[2][:4000], chars=len(rb[2])))


@bp.route("/api/mirror/note")
def note():
    wid, a, b = request.args.get("wid", ""), request.args.get("a", ""), request.args.get("b", "")
    if a not in LANGS or b not in LANGS or not re.fullmatch(r"Q\d+", wid):
        return jsonify(error="bad request"), 400
    ra, rb = _article(wid, a), _article(wid, b)
    if not ra or not rb:
        return jsonify(error="article not found"), 404
    A, B = f"{NAMES[a]} ({ra[0]})", f"{NAMES[b]} ({rb[0]})"
    prompt = (f"[A] {A}\n{ra[2][:rag.MAX_CHARS]}\n\n---\n\n[B] {B}\n{rb[2][:rag.MAX_CHARS]}")
    body = {"systemInstruction": {"parts": [{"text": NOTE_SYSTEM.format(A="A", B="B")}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700,
                                 "thinkingConfig": {"thinkingLevel": rag.THINKING}}}
    try:
        t0 = time.perf_counter()
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{rag.GEMINI_MODEL}:generateContent",
                          headers={"x-goog-api-key": rag._key()}, json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Gemini returned {r.status_code}: {r.text[:200]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        return jsonify(note="".join(p.get("text", "") for p in parts).strip(), ms=(time.perf_counter() - t0) * 1000)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 502

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Language mirror</title>
<style>
:root{--bg:#fff;--fg:#202122;--muted:#54595d;--line:#a2a9b1;--soft:#eaecf0;--card:#f8f9fa;--link:#3366cc;--a:#3366cc;--b:#e0801a;--sky:#f4f6fb}
@media(prefers-color-scheme:dark){:root{--bg:#101418;--fg:#eaecf0;--muted:#a2a9b1;--line:#54595d;--soft:#27292d;--card:#1b1f23;--link:#88a3e8;--a:#88a3e8;--b:#f0a050;--sky:#0b0e13}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--link);text-decoration:none}.tool{padding:12px 20px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font:400 1.5rem Georgia,serif;margin:0}.muted{color:var(--muted);font-size:.85rem}
select,input{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);padding:4px 8px;border-radius:2px}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 300px;height:calc(100vh - 130px)}
@media(max-width:900px){.wrap{grid-template-columns:1fr;height:auto}}
#stage{position:relative;background:var(--sky);min-height:420px}canvas{width:100%;height:100%;display:block;cursor:crosshair}
#tip{position:absolute;pointer-events:none;background:var(--bg);border:1px solid var(--line);padding:3px 8px;font-size:.8rem;display:none;white-space:nowrap}
aside{border-left:1px solid var(--line);overflow:auto;background:var(--card)}aside h2{margin:0;padding:8px 12px;font-size:.9rem;background:var(--soft);position:sticky;top:0}
aside li{list-style:none;padding:6px 12px;border-bottom:1px solid var(--soft);cursor:pointer;font-size:.85rem}aside li:hover{background:var(--soft)}aside ul{margin:0;padding:0}
.gap{float:right;color:var(--muted);font-variant-numeric:tabular-nums}
#detail{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;z-index:5}#detail .box{position:absolute;inset:4vh 4vw;background:var(--bg);border:1px solid var(--line);display:flex;flex-direction:column}
#detail .box>header{padding:12px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}.cols{display:grid;grid-template-columns:1fr 1fr;gap:0;overflow:auto;flex:1}
.col{padding:12px 16px;border-right:1px solid var(--soft);overflow:auto}.col h3{margin:0 0 4px;font:400 1.2rem Georgia,serif}.col pre{white-space:pre-wrap;font:inherit;margin:8px 0}
#note{padding:10px 16px;border-top:1px solid var(--line);background:var(--card);max-height:34vh;overflow:auto;white-space:pre-wrap}
.badge{font-size:.68rem;text-transform:uppercase;border:1px solid var(--line);padding:0 5px;margin-right:6px}
button{font:inherit;cursor:pointer;background:var(--card);color:inherit;border:1px solid var(--line);padding:3px 10px}
@media(max-width:700px){.cols{grid-template-columns:1fr}}
details{padding:6px 12px}pre.sql{font-size:.72rem;white-space:pre-wrap}
</style></head><body>
{{BANNER}}
<div class="tool"><h1>Language mirror</h1><span class="muted">How differently two Wikipedias tell the same story. Each line joins one topic's two articles; longer = further apart.</span>
<label>Compare <select id="a"></select> with <select id="b"></select></label><span class="muted" id="status"></span></div>
<div class="wrap"><div id="stage"><canvas id="c"></canvas><div id="tip"></div></div>
<aside><h2>Widest gaps</h2><div style="padding:6px 12px"><input id="flt" placeholder="filter by title" style="width:100%"></div><ul id="list"></ul>
<details><summary>The SQL MariaDB ran</summary><pre class="sql" id="sql"></pre></details></aside></div>
<div id="detail"><div class="box"><header><b id="dt"></b><button id="close">Close (Esc)</button></header>
<div class="cols"><div class="col" id="ca"></div><div class="col" id="cb"></div></div><div id="note">Writing the comparison note…</div></div></div>
<script>
const $=i=>document.getElementById(i),esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const L={fi:'Finnish',sv:'Swedish',en:'English'};
for(const s of ['a','b'])for(const k in L)$(s).add(new Option(L[k],k));
$('a').value='fi';$('b').value='en';
let data=null,items=[],view=[],maxGap=1,hover=-1,cv=$('c'),ctx=cv.getContext('2d'),W=0,H=0,dpr=1,pts;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function resize(){const r=cv.parentElement.getBoundingClientRect();dpr=devicePixelRatio||1;W=r.width;H=r.height;cv.width=W*dpr;cv.height=H*dpr;draw()}
const X=x=>(x*.5+.5)*(W-40)+20,Y=y=>(-y*.5+.5)*(H-40)+20;
function draw(){ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);if(!data)return;
 const ca=css('--a'),cb=css('--b'),fg=css('--fg');ctx.lineWidth=1;
 for(const it of view){const t=Math.min(1,it.gap/maxGap);ctx.strokeStyle=fg;ctx.globalAlpha=.04+.5*t*t;ctx.beginPath();ctx.moveTo(X(it.ax),Y(it.ay));ctx.lineTo(X(it.bx),Y(it.by));ctx.stroke()}
 ctx.globalAlpha=.7;for(const it of view){ctx.fillStyle=ca;ctx.fillRect(X(it.ax)-1,Y(it.ay)-1,2,2);ctx.fillStyle=cb;ctx.fillRect(X(it.bx)-1,Y(it.by)-1,2,2)}
 ctx.globalAlpha=1;if(hover>=0){const it=view[hover];ctx.lineWidth=2.5;ctx.strokeStyle=fg;ctx.beginPath();ctx.moveTo(X(it.ax),Y(it.ay));ctx.lineTo(X(it.bx),Y(it.by));ctx.stroke();
  ctx.fillStyle=ca;ctx.beginPath();ctx.arc(X(it.ax),Y(it.ay),5,0,7);ctx.fill();ctx.fillStyle=cb;ctx.beginPath();ctx.arc(X(it.bx),Y(it.by),5,0,7);ctx.fill()}}
function segDist(px,py,x1,y1,x2,y2){const dx=x2-x1,dy=y2-y1,l=dx*dx+dy*dy;let t=l?((px-x1)*dx+(py-y1)*dy)/l:0;t=Math.max(0,Math.min(1,t));return Math.hypot(px-x1-t*dx,py-y1-t*dy)}
function pick(e){const r=cv.getBoundingClientRect(),px=e.clientX-r.left,py=e.clientY-r.top;let best=-1,bd=9;
 for(let i=0;i<view.length;i++){const it=view[i];const d=segDist(px,py,X(it.ax),Y(it.ay),X(it.bx),Y(it.by))-Math.min(3,it.gap*4);if(d<bd){bd=d;best=i}}return best}
cv.addEventListener('mousemove',e=>{const i=pick(e);if(i!==hover){hover=i;draw()}const t=$('tip');
 if(i>=0){const it=view[i];t.style.display='block';t.style.left=Math.min(e.offsetX+12,W-220)+'px';t.style.top=(e.offsetY+12)+'px';t.textContent=it.ta+' / '+it.tb+'  ·  gap '+it.gap.toFixed(3)}else t.style.display='none'});
cv.addEventListener('click',e=>{const i=pick(e);if(i>=0)openPair(view[i])});
function render(){const f=$('flt').value.toLowerCase();view=items.filter(i=>!f||(i.ta+' '+i.tb).toLowerCase().includes(f));maxGap=Math.max(...view.map(i=>i.gap),.01);
 $('list').innerHTML=view.slice(0,200).map((it,k)=>`<li data-k="${k}"><span class="gap">${it.gap.toFixed(3)}</span>${esc(it.ta)}<br><span class="muted">${esc(it.tb)}</span></li>`).join('');
 draw()}
$('list').addEventListener('click',e=>{const li=e.target.closest('li');if(li)openPair(view[+li.dataset.k])});
$('flt').addEventListener('input',render);
async function load(){if($('a').value===$('b').value){$('status').textContent='Pick two different languages';return}
 $('status').textContent='Loading…';const r=await fetch(`/api/mirror/pairs?a=${$('a').value}&b=${$('b').value}`),d=await r.json();
 if(d.error){$('status').textContent=d.error;return}
 data=d;items=d.items;$('sql').textContent=d.sql;$('status').textContent=`${items.length} topics scored by MariaDB in ${Math.round(d.ms)} ms`;render()}
for(const s of ['a','b'])$(s).addEventListener('change',load);
addEventListener('resize',resize);
const sub=md=>md;
async function openPair(it){$('detail').style.display='block';$('dt').textContent=`${it.ta} / ${it.tb} — gap ${it.gap.toFixed(3)}`;
 $('ca').textContent=$('cb').textContent='Loading…';$('note').textContent='Writing the comparison note…';
 const q=`wid=${it.wid}&a=${data.a}&b=${data.b}`;
 fetch('/api/mirror/compare?'+q).then(r=>r.json()).then(d=>{for(const [id,x] of [['ca',d.a],['cb',d.b]])$(id).innerHTML=`<span class="badge">${x.lang}</span><a href="${esc(x.url)}" target="_blank" rel="noopener"><h3 style="display:inline">${esc(x.title)}</h3></a><div class="muted">${x.chars.toLocaleString()} characters${x.text.length<x.chars?' (first '+x.text.length.toLocaleString()+' shown)':''}</div><pre>${esc(x.text)}</pre>`});
 const r=await fetch('/api/mirror/note?'+q),d=await r.json();
 if(d.error){$('note').textContent=d.error;return}
 $('note').innerHTML='<b>What each says that the other leaves out</b> <span class="muted">(AI note from the two texts only; A = '+L[data.a]+', B = '+L[data.b]+')</span>\n\n'+esc(d.note)}
$('close').onclick=()=>$('detail').style.display='none';addEventListener('keydown',e=>{if(e.key==='Escape')$('close').click()});
resize();load();
</script></body></html>"""


@bp.route("/mirror")
def page():
    return Response(PAGE.replace("{{BANNER}}", hub.BANNER), mimetype="text/html")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["build"]:
        build()
    else:
        print("usage: python mirror.py build")
