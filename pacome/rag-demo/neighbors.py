"""Closest neighbours: pick any article, MariaDB returns its 10 nearest articles by vector, computed live."""
import time

from flask import Blueprint, Response, jsonify, request

import hub
import rag

bp = Blueprint("neighbors", __name__)
LANGS = ("fi", "sv", "en")
TOP = 10

NEIGHBORS_SQL = """\
SELECT a.lang, a.page_id, a.title, a.url, v.distance, LEFT(a.text, 280)
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS distance
      FROM article_vector
      {where}
      ORDER BY distance
      LIMIT ?) AS v
JOIN article a USING (lang, page_id)
ORDER BY v.distance"""


@bp.route("/api/neighbors/search")
def search_titles():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify(items=[])
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT lang, page_id, title FROM article WHERE title LIKE ? "
                    "ORDER BY (title = ?) DESC, (title LIKE ?) DESC, CHAR_LENGTH(title) LIMIT 12",
                    (f"%{like}%", q, f"{like}%"))
        return jsonify(items=[dict(lang=l, page_id=p, title=t) for l, p, t in cur.fetchall()])
    finally:
        conn.close()


@bp.route("/api/neighbors")
def neighbors():
    lang = request.args.get("lang", "")
    scope = request.args.get("scope", "")
    try:
        page_id = int(request.args.get("page_id", ""))
    except ValueError:
        return jsonify(error="bad page_id"), 400
    if lang not in LANGS or scope not in ("", *LANGS):
        return jsonify(error="bad language"), 400
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT a.title, a.url, VEC_ToText(v.embedding) FROM article a JOIN article_vector v "
                    "USING (lang, page_id) WHERE lang = ? AND page_id = ?", (lang, page_id))
        row = cur.fetchone()
        if not row:
            return jsonify(error="article not found"), 404
        title, url, vec = row
        sql = NEIGHBORS_SQL.format(where="WHERE lang = ?" if scope else "")
        params = [vec] + ([scope] if scope else []) + [TOP + 1]
        t0 = time.perf_counter()
        cur.execute(f"SET SESSION mhnsw_ef_search = {rag.EF_SEARCH}")
        cur.execute(sql, params)
        rows = cur.fetchall()
        ms = (time.perf_counter() - t0) * 1000
    finally:
        conn.close()
    items = [dict(lang=l, page_id=p, title=t, url=u, distance=round(float(d), 4), snippet=s)
             for l, p, t, u, d, s in rows if not (l == lang and p == page_id)][:TOP]
    return jsonify(source=dict(lang=lang, page_id=page_id, title=title, url=url), items=items, ms=ms, sql=sql)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neighbour graph</title>""" + hub.STYLE + r"""
<style>
:root{--fi:#3366cc;--sv:#e0b020;--en:#d0452a;--sky:#f4f6fb}
@media(prefers-color-scheme:dark){:root{--fi:#7b9be8;--sv:#f0c850;--en:#f0705a;--sky:#0b0e13}}
body{display:flex;flex-direction:column;height:100vh}
.bar{display:flex;gap:10px;align-items:center;padding:8px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap;position:relative}
.bar h1{font:400 1.3rem Georgia,serif;margin:0 6px 0 0}
input,select,button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);padding:5px 9px;border-radius:2px}button{cursor:pointer}
#q{width:min(340px,100%)}.sug{position:absolute;top:100%;left:120px;z-index:9;background:var(--bg);border:1px solid var(--line);display:none;min-width:340px}
.sug div{padding:6px 10px;cursor:pointer}.sug div:hover{background:var(--card)}
.wrap{flex:1;display:grid;grid-template-columns:minmax(0,1fr) 320px;min-height:0}
@media(max-width:900px){.wrap{grid-template-columns:1fr;grid-template-rows:60vh auto}}
#stage{position:relative;background:var(--sky);min-height:300px}canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
#tip{position:absolute;pointer-events:none;background:var(--bg);border:1px solid var(--line);padding:3px 8px;font-size:.8rem;display:none;max-width:280px}
aside{border-left:1px solid var(--line);overflow:auto;background:var(--card);padding:0 0 20px}aside h2{margin:0;padding:8px 12px;font-size:.95rem;background:var(--card)}
aside .sec{padding:10px 14px;border-bottom:1px solid var(--line)}.snip{color:var(--muted);font-size:.85rem}
aside li{list-style:none;padding:5px 14px;cursor:pointer;font-size:.85rem;border-bottom:1px solid var(--line)}aside li:hover{background:var(--bg)}aside ul{margin:0;padding:0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}.d{float:right;color:var(--muted);font-variant-numeric:tabular-nums}
.legend{margin-left:auto;font-size:.8rem;color:var(--muted)}pre{white-space:pre-wrap;font-size:.72rem;margin:6px 0 0}
</style></head><body>""" + hub.BANNER + r"""
<div class="bar"><h1>Neighbour graph</h1><input id="q" placeholder="Start from an article, e.g. Talvisota" autocomplete="off">
<select id="scope"><option value="">all languages</option><option value="fi">Finnish only</option><option value="sv">Swedish only</option><option value="en">English only</option></select>
<button id="reset">Clear</button><span class="legend"><span class="dot" style="background:var(--fi)"></span>fi <span class="dot" style="background:var(--sv)"></span>sv <span class="dot" style="background:var(--en)"></span>en · click a node to centre it, drag to move, scroll to zoom</span>
<div class="sug" id="sug"></div></div>
<div class="wrap"><div id="stage"><canvas id="c"></canvas><div id="tip"></div></div>
<aside><div class="sec" id="info"><span class="snip">Search for an article to plant the first node.</span></div>
<h2>10 closest</h2><ul id="list"></ul>
<details class="sec"><summary>The SQL MariaDB ran</summary><pre id="sql"></pre><div class="snip" id="ms"></div></details></aside></div>
<script>
const $=i=>document.getElementById(i),esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const cv=$('c'),ctx=cv.getContext('2d');let W=0,H=0,dpr=1;
let nodes=new Map(),edges=[],edgeSet=new Set(),center=null,hoverN=null,seq=0;
let cam={x:0,y:0,k:1},target={x:0,y:0},alpha=1,running=false,follow=true;
const key=(l,p)=>l+':'+p;
function resize(){const r=cv.parentElement.getBoundingClientRect();dpr=devicePixelRatio||1;W=r.width;H=r.height;cv.width=W*dpr;cv.height=H*dpr;kick()}
addEventListener('resize',resize);
const toScreen=(x,y)=>[(x-cam.x)*cam.k+W/2,(y-cam.y)*cam.k+H/2],toWorld=(sx,sy)=>[(sx-W/2)/cam.k+cam.x,(sy-H/2)/cam.k+cam.y];
function addNode(o,nearX,nearY){const k=key(o.lang,o.page_id);let n=nodes.get(k);if(n)return n;
 const a=Math.random()*6.283;n={k,lang:o.lang,page_id:o.page_id,title:o.title,url:o.url,snippet:o.snippet||'',x:nearX+Math.cos(a)*40,y:nearY+Math.sin(a)*40,vx:0,vy:0,order:seq++,fixed:false,loaded:false};nodes.set(k,n);return n}
function addEdge(a,b,d){const id=a.k<b.k?a.k+'|'+b.k:b.k+'|'+a.k;if(edgeSet.has(id))return;edgeSet.add(id);edges.push({a,b,d})}
function prune(){if(nodes.size<=140)return;const adj=new Set([center.k]);const deg=new Map();
 for(const e of edges){deg.set(e.a.k,(deg.get(e.a.k)||0)+1);deg.set(e.b.k,(deg.get(e.b.k)||0)+1);if(e.a===center)adj.add(e.b.k);if(e.b===center)adj.add(e.a.k)}
 const victims=[...nodes.values()].filter(n=>!adj.has(n.k)&&(deg.get(n.k)||0)<=1).sort((a,b)=>a.order-b.order);
 for(const v of victims){if(nodes.size<=120)break;nodes.delete(v.k)}
 edges=edges.filter(e=>nodes.has(e.a.k)&&nodes.has(e.b.k));edgeSet=new Set(edges.map(e=>e.a.k<e.b.k?e.a.k+'|'+e.b.k:e.b.k+'|'+e.a.k))}
async function focus(lang,pid,info){const scope=$('scope').value;
 const d=await (await fetch(`/api/neighbors?lang=${lang}&page_id=${pid}&scope=${scope}`)).json();
 if(d.error){$('info').textContent=d.error;return}
 const src=addNode(d.source,0,0);if(!nodes.size||!center){src.x=0;src.y=0}
 src.loaded=true;center=src;const prev=nodes.get(key(lang,pid));
 for(const it of d.items){const n=addNode(it,src.x,src.y);addEdge(src,n,it.distance);if(!n.snippet)n.snippet=it.snippet}
 follow=true;src.snippet=src.snippet||'';prune();target={x:src.x,y:src.y};alpha=.7;kick();
 history.replaceState(null,'',`/neighbors?lang=${lang}&page_id=${pid}`+(scope?'&scope='+scope:''));
 $('info').innerHTML=`<span class="dot" style="background:var(--${src.lang})"></span><b>${esc(src.title)}</b><div class="snip"><a href="${esc(d.source.url)}" target="_blank" rel="noopener">Open on Wikipedia ↗</a></div><div class="snip" style="margin-top:6px">${esc((src.snippet||'').replace(/^#.*\n/,''))}</div>`;
 $('list').innerHTML=d.items.map(i=>`<li data-l="${i.lang}" data-p="${i.page_id}"><span class="d">${i.distance.toFixed(3)}</span><span class="dot" style="background:var(--${i.lang})"></span>${esc(i.title)}</li>`).join('');
 $('sql').textContent='SET SESSION mhnsw_ef_search = 80;\n\n'+d.sql;$('ms').textContent=`${d.items.length} neighbours in ${Math.round(d.ms)} ms`}
$('list').addEventListener('click',e=>{const li=e.target.closest('li');if(li)focus(li.dataset.l,+li.dataset.p)});
// physics
function step(){const arr=[...nodes.values()],n=arr.length;
 for(let i=0;i<n;i++){const a=arr[i];for(let j=i+1;j<n;j++){const b=arr[j];let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy+.01;if(d2>90000)continue;const f=1800/d2,d=Math.sqrt(d2);dx/=d;dy/=d;a.vx+=dx*f;a.vy+=dy*f;b.vx-=dx*f;b.vy-=dy*f}}
 for(const e of edges){const dx=e.b.x-e.a.x,dy=e.b.y-e.a.y,d=Math.hypot(dx,dy)||1,rest=70+e.d*380,f=(d-rest)*.012;const fx=dx/d*f,fy=dy/d*f;e.a.vx+=fx;e.a.vy+=fy;e.b.vx-=fx;e.b.vy-=fy}
 for(const a of arr){a.vx-=a.x*.001;a.vy-=a.y*.001;if(a.fixed){a.vx=a.vy=0;continue}const sp=Math.hypot(a.vx,a.vy);if(sp>12){a.vx*=12/sp;a.vy*=12/sp}a.x+=a.vx*alpha;a.y+=a.vy*alpha;a.vx*=.3;a.vy*=.3}}
function neighborsOfCenter(){const s=new Set();if(!center)return s;for(const e of edges){if(e.a===center)s.add(e.b.k);if(e.b===center)s.add(e.a.k)}return s}
function draw(){ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);const fg=css('--fg'),adj=neighborsOfCenter();
 for(const e of edges){const hot=e.a===center||e.b===center;const [x1,y1]=toScreen(e.a.x,e.a.y),[x2,y2]=toScreen(e.b.x,e.b.y);
  ctx.strokeStyle=fg;ctx.globalAlpha=hot?.55:.14;ctx.lineWidth=hot?Math.max(1,3*(1-e.d)):1;ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke()}
 ctx.globalAlpha=1;ctx.font='12px sans-serif';ctx.textBaseline='middle';
 for(const n of nodes.values()){const [x,y]=toScreen(n.x,n.y),isC=n===center,isN=adj.has(n.k),r=(isC?13:isN?8:5)*Math.min(1.4,Math.max(.7,cam.k));
  ctx.globalAlpha=isC||isN||n===hoverN?1:.55;ctx.fillStyle=css('--'+n.lang);ctx.beginPath();ctx.arc(x,y,r,0,7);ctx.fill();
  if(isC||n===hoverN){ctx.strokeStyle=fg;ctx.lineWidth=2.5;ctx.stroke()}
  if(isC||isN||n===hoverN||cam.k>1.2){ctx.globalAlpha=isC||isN||n===hoverN?1:.7;ctx.fillStyle=fg;ctx.font=(isC?'bold 14px':'12px')+' sans-serif';const t=n.title.length>34?n.title.slice(0,33)+'…':n.title;ctx.fillText(t,x+r+5,y)}}
 ctx.globalAlpha=1}
function kick(){alpha=Math.max(alpha,.12);if(!running){running=true;requestAnimationFrame(loop)}}
function loop(){step();alpha*=.94;cam.x+=(target.x-cam.x)*.1;cam.y+=(target.y-cam.y)*.1;
 if(center&&!drag&&follow){target.x=center.x;target.y=center.y}draw();
 if(alpha>.01||Math.hypot(target.x-cam.x,target.y-cam.y)>.5||drag)requestAnimationFrame(loop);else running=false}
// interaction
let drag=null,panning=null,moved=0;
function hit(sx,sy){const [wx,wy]=toWorld(sx,sy);let best=null,bd=1e9;for(const n of nodes.values()){const d=Math.hypot(n.x-wx,n.y-wy)*cam.k;const r=n===center?16:11;if(d<r&&d<bd){bd=d;best=n}}return best}
cv.addEventListener('mousedown',e=>{moved=0;const n=hit(e.offsetX,e.offsetY);if(n){drag=n;n.fixed=true}else{follow=false;panning={x:e.offsetX,y:e.offsetY,cx:cam.x,cy:cam.y}}kick()});
addEventListener('mousemove',e=>{const r=cv.getBoundingClientRect(),sx=e.clientX-r.left,sy=e.clientY-r.top;
 if(drag){moved++;const [wx,wy]=toWorld(sx,sy);drag.x=wx;drag.y=wy;alpha=Math.max(alpha,.1);kick();return}
 if(panning){moved++;cam.x=panning.cx-(sx-panning.x)/cam.k;cam.y=panning.cy-(sy-panning.y)/cam.k;target.x=cam.x;target.y=cam.y;kick();return}
 if(sx<0||sy<0||sx>W||sy>H)return;const n=hit(sx,sy);if(n!==hoverN){hoverN=n;kick()}const t=$('tip');
 if(n){t.style.display='block';t.style.left=Math.min(sx+14,W-290)+'px';t.style.top=(sy+14)+'px';t.textContent=n.title+' ('+n.lang+')'}else t.style.display='none'});
addEventListener('mouseup',e=>{if(drag){const n=drag;n.fixed=false;drag=null;if(moved<4)focus(n.lang,n.page_id);kick()}panning=null});
cv.addEventListener('wheel',e=>{e.preventDefault();const k=Math.min(3,Math.max(.3,cam.k*(e.deltaY<0?1.12:.89)));cam.k=k;kick()},{passive:false});
// search + controls
let timer;$('q').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(async()=>{const q=$('q').value.trim();if(q.length<2){$('sug').style.display='none';return}
 const d=await (await fetch('/api/neighbors/search?q='+encodeURIComponent(q))).json();
 $('sug').innerHTML=d.items.map(i=>`<div data-l="${i.lang}" data-p="${i.page_id}"><span class="dot" style="background:var(--${i.lang})"></span>${esc(i.title)}</div>`).join('');$('sug').style.display=d.items.length?'block':'none'},200)});
$('sug').addEventListener('click',e=>{const d=e.target.closest('div');if(!d)return;$('sug').style.display='none';$('q').value='';reset();focus(d.dataset.l,+d.dataset.p)});
function reset(){nodes=new Map();edges=[];edgeSet=new Set();center=null;cam={x:0,y:0,k:1};target={x:0,y:0};$('list').innerHTML='';kick()}
$('reset').onclick=()=>{reset();$('info').innerHTML='<span class="snip">Search for an article to plant the first node.</span>';history.replaceState(null,'','/neighbors')};
$('scope').addEventListener('change',()=>center&&focus(center.lang,center.page_id));
resize();const u=new URLSearchParams(location.search);if(u.get('page_id')){$('scope').value=u.get('scope')||'';focus(u.get('lang'),+u.get('page_id'))}
</script></body></html>"""


@bp.route("/neighbors")
def page():
    return Response(PAGE, mimetype="text/html")
