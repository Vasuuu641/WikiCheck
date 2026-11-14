"""Opinion drift: how the top articles for a question changed their stance on shared claims over time.

  GET /drift                    the page
  GET /api/drift/stream?q=...   SSE; q is a question, or an exact article title (then &lang= may name its edition);
                                with fixture=1 replays fixture.json, else runs stance.run(q) in a thread
                                with keep-alive comments every HEARTBEAT seconds
  GET /api/drift/asof           article text at a date (history.text_asof), with the SQL shown
  GET /api/drift/evidence       the paragraph carrying a claim at a version (stance.evidence)

Events: sources, claims, probes, change, summary, done | problem.
"""
import functools
import json
import queue
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment

from . import history, stance

router = APIRouter()
FIXTURE_PATH = Path(__file__).parent / "fixture.json"
MAX_QUESTION = 300
HEARTBEAT = 10          # seconds of silence after which a keep-alive comment is sent (proxies cut idle streams)
EXAMPLES = [
    "Why did the Finnish Civil War start in 1918?",
    "Who started the Winter War?",
    "Kuka oli Suomen ensimmäinen presidentti ja milloin hänet valittiin?",
    "Vad hände med Åland efter första världskriget?",
]


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _fixture_events(fast=False):
    fixture = json.loads(FIXTURE_PATH.read_text())
    for event, data in fixture:
        yield sse(event, data)
        if not fast:
            time.sleep((2.2 if data["round"] == 0 else 1.2) if event == "probes" else 0.25)


def _pump(question, out, stop, lang=None):
    """Run the engine in a thread so the stream can send keep-alives while a slow step (Gemini, history load) runs."""
    try:
        for event, data in stance.run(question, lang):
            if stop.is_set():       # the reader went away: stop at the next event instead of scoring the rest
                return
            out.put(sse(event, data))
    except Exception as e:
        out.put(sse("problem", f"{type(e).__name__}: {e}"))
    finally:
        out.put(None)


@router.get("/api/drift/stream")
def stream(q: str = "", fixture: str = "", fast: str = "", lang: str = ""):
    q, lang = q.strip(), lang.strip().lower()
    lang = lang if len(lang) == 2 and lang.isalpha() else None

    def events():
        try:
            if fixture:
                yield from _fixture_events(bool(fast))
                return
            if not q:
                raise ValueError("Empty question")
            if len(q) > MAX_QUESTION:
                raise ValueError(f"The question is too long ({len(q)} characters, at most {MAX_QUESTION})")
        except Exception as e:
            yield sse("problem", f"{type(e).__name__}: {e}")
            return
        out, stop = queue.Queue(), threading.Event()
        threading.Thread(target=_pump, args=(q, out, stop, lang), daemon=True).start()
        try:
            while True:
                try:
                    item = out.get(timeout=HEARTBEAT)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    return
                yield item
        finally:
            stop.set()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/drift/asof")
def asof(page_id: int = 0, date: str = ""):
    try:
        rev_id, text, sql = history.text_asof(page_id, date)
        return {"rev_id": rev_id, "text": text, "sql": sql}
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@functools.lru_cache(maxsize=256)
def _evidence(page_id, rev_id, claim):
    """Each lookup costs several Decisions requests: remember the answer for the life of the server."""
    return stance.evidence(page_id, rev_id, claim)


@router.get("/api/drift/evidence")
def evidence(page_id: int = 0, rev_id: int = 0, claim: str = ""):
    claim = claim.strip()
    if not claim:
        return JSONResponse({"error": "missing claim"}, status_code=400)
    try:
        return _evidence(page_id, rev_id, claim)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)


@router.get("/drift", response_class=HTMLResponse)
def page(q: str = "", fixture: str = "", lang: str = ""):
    return _TEMPLATE.render(q=q.strip(), lang=lang.strip()[:3], fixture=bool(fixture), examples=EXAMPLES)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aikakone · Opinion drift</title>
<style>
/* Same look as the Aikakone extension: Wikipedia's own type and colours, the two diff colours for
   "what it said" (yellow) and "what it says now" (blue). The ribbon colours encode a stance scale. */
:root {
  --paper:#ffffff; --wash:#f8f9fa; --ink:#202122; --muted:#54595d; --rule:#a2a9b1; --soft:#eaecf0;
  --link:#3366cc; --this:#feeec8; --this-edge:#f0c14b; --other:#d8ecff; --other-edge:#6fa8dc;
  --alert:#b32424; --good:#14866d;
  --fact:#1f5fbf; --attr:#9dc3ea; --disp:#f4a35f; --deny:#d1490f;
  /* names the chart script uses */
  --bg:var(--paper); --fg:var(--ink); --line:var(--rule); --card:var(--wash); --accent:var(--link); --red:#d33; --green:var(--good);
  --serif:"Linux Libertine","Libertinus Serif",Georgia,"Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
* { box-sizing:border-box }
html, body { margin:0 }
body { background:var(--paper); color:var(--ink); font:14px/1.55 var(--sans) }
a { color:var(--link); text-decoration:none } a:hover { text-decoration:underline }
button { font:inherit; cursor:pointer }
:focus-visible { outline:2px solid var(--link); outline-offset:1px }
[hidden] { display:none !important }
.wrap { max-width:1240px; margin:0 auto; padding:0 clamp(16px,3vw,32px) }

header { border-bottom:1px solid var(--rule) }
header .wrap { padding-top:16px; padding-bottom:14px }
.wordmark { font:400 14px/1 var(--serif); letter-spacing:.04em; color:var(--muted) }
h1 { margin:6px 0 4px; font:400 30px/1.2 var(--serif) }
.lede { margin:0; color:var(--muted); max-width:72ch }

main.wrap { padding-top:18px; padding-bottom:48px }
form { display:flex; max-width:680px }
input[type=text] { flex:1; min-width:0; padding:7px 10px; font:inherit; font-size:15px; color:inherit; background:var(--paper);
  border:1px solid var(--rule); border-right:0; border-radius:2px 0 0 2px; outline:none }
input[type=text]:focus { border-color:var(--link); box-shadow:inset 0 0 0 1px var(--link) }
form button { padding:7px 18px; font-weight:600; color:#fff; background:var(--link); border:1px solid var(--link); border-radius:0 2px 2px 0 }
form button:hover { background:#2a4b8d; border-color:#2a4b8d } form button:disabled { opacity:.6; cursor:wait }
.quiet { padding:0; color:var(--link); background:none; border:0 } .quiet:hover { text-decoration:underline }
.ex { margin:8px 0 0; font-size:13px; color:var(--muted) } .ex .quiet { margin-left:14px; text-align:left }

.steps { list-style:none; padding:0; margin:14px 0 0; font-size:13px; color:var(--muted) }
.steps li { padding:1px 0 } .steps li.done { color:var(--ink) }
.steps li::before { content:''; display:inline-block; width:1.4em }
.steps li.done::before { content:'\2713'; color:var(--good); font-weight:700 }
.steps li.err { color:var(--alert) } .steps li.err::before { content:'\2717'; font-weight:700 }
.steps li.run::before { width:.75em; height:.75em; margin-right:.65em; vertical-align:-1px; border:2px solid var(--soft);
  border-top-color:var(--link); border-radius:50%; animation:spin .8s linear infinite }
.steps li.run[data-t]::after { content:', ' attr(data-t) ' s'; font-variant-numeric:tabular-nums }
@keyframes spin { to { transform:rotate(360deg) } }
@media (prefers-reduced-motion: reduce) { .steps li.run::before, #loadbar i { animation-duration:3s } }
.status { margin:10px 0 0; color:var(--muted) } .status b { color:var(--ink) }
#loadbar { height:2px; background:var(--soft); overflow:hidden; position:relative; margin:10px 0 0; display:none; max-width:680px }
#loadbar.on { display:block }
#loadbar i { position:absolute; left:0; top:0; height:2px; width:38%; background:var(--link); animation:slide 1.15s ease-in-out infinite }
@keyframes slide { 0% { transform:translateX(-100%) } 100% { transform:translateX(320%) } }
.err { margin:12px 0; padding:8px 12px; color:var(--alert); background:#fee7e6; border-left:3px solid var(--alert) }

.grid { display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:36px; align-items:start; margin-top:18px }
@media (max-width:960px) { .grid { grid-template-columns:1fr } aside.panel { position:static !important; max-height:none !important; padding-left:0 !important; border-left:0 !important } }
h2 { margin:26px 0 6px; padding-bottom:3px; font:400 19px/1.3 var(--serif); border-bottom:1px solid var(--soft) }

.summary { margin-bottom:6px; font:400 16px/1.65 var(--sans); max-width:72ch }
.summary p { margin:0 0 8px } .summary b { font:600 12px var(--sans); color:var(--muted); margin-right:4px }
.summary .ai { display:block; font:12px var(--sans); color:var(--muted) }
#srcwrap > h2 { margin-top:10px }
ol.src { margin:4px 0 0; padding-left:22px } ol.src li { padding:1px 0 } ol.src li::marker { color:var(--muted) }
.meta { color:var(--muted); font-size:12.5px } p.meta { margin:0 }

/* the changes of position: same timeline as the extension */
.timeline { margin:8px 0 0; padding:0; list-style:none; border-left:2px solid var(--soft); max-width:78ch }
.timeline li { position:relative; padding:9px 0 9px 16px }
.timeline li::before { content:""; position:absolute; left:-6px; top:15px; width:8px; height:8px; border-radius:50%;
  background:var(--paper); border:2px solid var(--rule) }
.timeline li.sel::before { background:var(--link); border-color:var(--link) }
.timeline .empty::before { display:none }
.when { font-size:12.5px; color:var(--muted) }
.move { display:block } a.claim { font-weight:600; color:var(--ink) } li.sel a.claim, a.claim:hover { color:var(--link) }
/* stance chips: the chart's colours (stated as fact, attributed, not addressed, disputed, denied) */
.stance { padding:0 4px; border-radius:2px; white-space:nowrap }
.st-fact { background:var(--fact); color:#fff } .st-attr { background:var(--attr); color:#202122 }
.st-none { background:transparent; color:#54595d; box-shadow:inset 0 0 0 1px #a2a9b1 }
.st-disp { background:var(--disp); color:#202122 } .st-deny { background:var(--deny); color:#fff }
.edit-note { display:block; margin-top:2px; font-size:12.5px; color:var(--muted) }

/* the charts */
.tools { display:flex; gap:18px; align-items:center; flex-wrap:wrap; margin:0 0 8px; font-size:13px; color:var(--muted) }
.tools label { cursor:pointer }
#sparkwrap { display:flex; flex-wrap:wrap; gap:6px 18px; margin:4px 0 10px }
.spark { padding:2px 0 4px; font-size:12px; cursor:pointer; border-bottom:2px solid transparent }
.spark b { display:block; font-weight:400; color:var(--link) } .spark:hover b { text-decoration:underline }
.spark.sel { border-bottom-color:var(--ink) } .spark.sel b { color:var(--ink); font-weight:600 }
#strips { position:relative; overflow-x:auto; border-top:1px solid var(--soft) } #strips svg { min-width:760px }
#strips.pending { min-height:180px; background:repeating-linear-gradient(0deg,var(--paper) 0 58px,var(--soft) 58px 59px) }
svg text { font:11px var(--sans); fill:var(--muted) }
.lane, .tick { cursor:pointer } .tick:focus { outline:none } circle.tick:focus-visible { stroke:var(--ink); stroke-width:2 }
.cursorline { stroke:var(--link); stroke-width:1.5; cursor:ew-resize }
.legend { display:flex; gap:6px 16px; flex-wrap:wrap; font-size:12.5px; color:var(--muted); margin:8px 0 0 }
.legend i { display:inline-block; width:20px; height:10px; margin-right:5px; vertical-align:-1px; border:1px solid var(--rule) }

/* the evidence panel */
aside.panel { position:sticky; top:16px; max-height:calc(100vh - 32px); overflow-y:auto; padding-left:20px; border-left:1px solid var(--soft); font-size:13.5px }
aside.panel h2 { margin-top:0 }
.muted { color:var(--muted) }
.quote { margin:6px 0; padding:5px 10px; border-left:3px solid var(--soft); font:400 14px/1.55 var(--sans); overflow-wrap:anywhere }
.quote.del { background:var(--this); border-color:var(--this-edge) }
.quote.add { background:var(--other); border-color:var(--other-edge) }
.kv { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; margin-top:8px; font-size:13px } .kv b { font-weight:400; color:var(--muted) }
pre { margin:8px 0; padding:8px; overflow-x:auto; font:11px/1.45 ui-monospace,Menlo,Consolas,monospace; background:var(--wash); border:1px solid var(--soft) }
#asoftext { max-height:360px; overflow-y:auto; white-space:pre-wrap; font:400 13.5px/1.6 var(--sans) }
p.err { margin:6px 0 }

footer { margin-top:40px; padding-top:10px; border-top:1px solid var(--soft); color:var(--muted); font-size:12.5px; max-width:90ch }
</style></head>
<body>
<header><div class="wrap">
  <div class="wordmark">Aikakone</div>
  <h1 id="title">Opinion drift</h1>
  <p class="lede">How Finnish Wikipedia's position on the same claims changed over the years, down to the edit that changed it.</p>
</div></header>
<main class="wrap">
<form id="f"><input type="text" id="q" aria-label="Article or question" placeholder="An article, or a question such as Who started the Winter War?" value="{{ q }}" autofocus>
<button id="go" type="submit">Trace</button></form>
<p class="ex" id="ex">Try{% for e in examples %}<button type="button" class="quiet" data-q="{{ e }}">{{ e }}</button>{% endfor %}<button type="button" class="quiet" data-fixture="1">A recorded run</button></p>
<ul class="steps" id="steps" hidden>
  <li id="st-load">Reading the edit histories</li>
  <li id="st-claims">Writing the claims to follow</li>
  <li id="st-grid">Reading old versions against every claim</li>
  <li id="st-bisect">Narrowing down the edits</li>
</ul>
<p class="status" id="status" hidden></p>
<div id="loadbar"><i></i></div>
<div class="err" id="err" hidden></div>
<div class="grid">
<section>
  <div class="summary" id="summary" hidden></div>
  <div id="srcwrap" hidden><h2>Articles followed</h2>
    <p class="meta">Numbered as the lanes in each row of the chart.</p><ol id="srclist" class="src"></ol></div>
  <h2 id="charthead" hidden>Position over time</h2>
  <div class="tools" id="tools" hidden>
    <label><input type="checkbox" id="onlychg"> Only claims that changed</label>
    <button type="button" class="quiet" id="dl">Download the results (JSON)</button>
    <button type="button" class="quiet" id="cp">Copy link</button>
  </div>
  <div id="sparkwrap"></div>
  <div id="strips"></div>
  <div class="legend" id="legend" hidden>
    <span><i style="background:var(--fact)"></i>Stated as fact</span>
    <span><i style="background:var(--attr)"></i>Attributed or hedged</span>
    <span><i style="background:transparent"></i>Not addressed</span>
    <span><i style="background:var(--disp)"></i>Presented as disputed</span>
    <span><i style="background:var(--deny)"></i>Denied</span>
    <span><i style="background:repeating-linear-gradient(45deg,var(--line) 0 2px,transparent 2px 6px)"></i>Edit war</span>
    <span>Fainter means less present. A dot marks the exact edit that changed a position.</span>
  </div>
  <h2 id="chghead" hidden>Changes of position</h2>
  <ol class="timeline" id="headlines"></ol>
</section>
<aside class="panel"><h2>Evidence</h2><div id="proof">
<p class="muted">Choose a change, a dot or a coloured cell to see the edit and its text, or drag the blue line across
the chart to read an article as it stood on that day.</p></div></aside>
</div>
<footer>Text from Wikipedia, CC BY-SA 4.0. Every version is read against the same claims by the Gemini Decisions API, and each
change is located by binary search over MariaDB's system-versioned history. Positions are machine-judged: check the edit
before relying on it. Finnish articles only; versions are read up to 16,000 characters.</footer>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = t => String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
let Y0 = 2002, Y1 = 2026;
const MS = 86400000;
const tms = t => Date.parse(t);
const COL = { 2:'var(--fact)', 1:'var(--attr)', [-1]:'var(--disp)', [-2]:'var(--deny)' };
function bucket(s, p) { if (p < 0.5) return 0; return s > 0.6 ? 2 : s > 0.15 ? 1 : s < -0.6 ? -2 : s < -0.15 ? -1 : 0; }
const LANE_H = 10, Lanes = () => S.articles.length || 5;
const ROW_H = () => Math.max(Lanes() * LANE_H + 20, 40);   // room for a two-line claim label
function wrap(text, max, lines) {
  const words = String(text).split(' '), out = []; let cur = '';
  for (const w of words) {
    if ((cur + ' ' + w).trim().length > max) { out.push(cur.trim()); cur = w; if (out.length === lines) break; }
    else cur = (cur + ' ' + w).trim();
  }
  if (out.length < lines && cur) out.push(cur);
  if (cur && out.length === lines && words.join(' ').length > out.join(' ').length) out[lines - 1] += '…';
  return out;
}

const S = { articles: [], claims: [], probes: {}, changes: [], rounds: [], done: null, running: false };
window.DRIFT = S; // also used by console-driven checks
// probes[a][cid] = sorted list of [t, stance, presence, rev_id]
function addProbe(it) {
  const a = S.probes[it.a] || (S.probes[it.a] = {});
  for (const cid in it.s) {
    const list = a[cid] || (a[cid] = []);
    if (!list.some(x => x[3] === it.rev_id)) list.push([tms(it.t), it.s[cid][0], it.s[cid][1], it.rev_id]);
    list.sort((x, y) => x[0] - y[0]);
  }
}
function volatility(cid) {
  let n = 0;
  for (const a of Object.keys(S.probes)) {
    const list = S.probes[a][cid]; if (!list) continue;
    let prev = null;
    for (const [t, s, p] of list) { const b = bucket(s, p); if (prev !== null && b !== prev) n++; prev = b; }
  }
  return n;
}
const rowOrder = () => S.claims.filter(c => !$('onlychg').checked || S.changes.some(x => x.claim === c.id))
  .sort((a, b) => volatility(b.id) - volatility(a.id));

let es = null, tickTimer = 0, tStart = 0;
const setStep = (id, state) => { $(id).className = state; };
function stepText(id, txt, state) { const e = $(id); if (txt) e.textContent = txt; if (state !== undefined) e.className = state; }
const STEP_TEXT = {};
['st-load','st-claims','st-grid','st-bisect'].forEach(i => STEP_TEXT[i] = $(i).textContent);
function start(q, fixture) {
  if (es) es.close();
  for (const i in STEP_TEXT) $(i).textContent = STEP_TEXT[i];
  S.articles = []; S.claims = []; S.probes = {}; S.changes = []; S.rounds = []; S.done = null; S.selChange = null; S.q = q;
  $('headlines').innerHTML = ''; $('summary').hidden = true; $('summary').innerHTML = ''; S.summary = null; $('sparkwrap').innerHTML = ''; $('err').hidden = true;
  ['st-load','st-claims','st-grid','st-bisect'].forEach(i => setStep(i, ''));
  stepText('st-load', null, 'run'); $('steps').hidden = false; $('status').hidden = false; $('status').textContent = 'Connecting…';
  $('strips').classList.add('pending'); $('strips').innerHTML = ''; $('loadbar').classList.add('on');
  S.running = true; tStart = Date.now(); $('go').disabled = true;
  clearInterval(tickTimer); tickTimer = setInterval(() => {
    if (S.running && !S.done) { const e = ((Date.now() - tStart) / 1000);
      const el = document.querySelector('.steps li.run'); if (el) el.dataset.t = e.toFixed(0); } }, 500);
  $('title').textContent = fixture ? 'A recorded run' : (q || 'Opinion drift');
  document.title = 'Aikakone · ' + $('title').textContent;
  es = new EventSource('/api/drift/stream?fixture=' + (fixture ? '1' : '') + (FAST ? '&fast=1' : '') + '&q=' + encodeURIComponent(q || '') +
                       (q && q === PARAM_Q && PARAM_LANG ? '&lang=' + encodeURIComponent(PARAM_LANG) : ''));
  es.addEventListener('sources', e => { const d = JSON.parse(e.data); S.articles = d.articles; setAxis(d.articles); $('tools').hidden = false;
    setStep('st-load', 'done'); stepText('st-claims', null, 'run');
    $('srclist').innerHTML = d.articles.map(a => '<li id="src' + a.i + '"><a href="' + esc(a.url) +
      '" target="_blank" rel="noopener">' + esc(a.title) + '</a> <span class="meta">' + a.n_revs.toLocaleString('en') +
      ' versions, ' + esc(String(a.first).slice(0, 4)) + ' to ' + esc(String(a.last).slice(0, 4)) + '</span></li>').join('') +
      ((d.timings && d.timings.dropped_far) ? '<li class="meta" style="list-style:none">' + d.timings.dropped_far + ' further article(s) left out: too far from your question.</li>' : '');
    $('srcwrap').hidden = false; $('charthead').hidden = false; $('legend').hidden = false;
    $('status').textContent = d.articles.length + ' article' + (d.articles.length === 1 ? '' : 's') + ', ' +
      d.articles.reduce((s, a) => s + a.n_revs, 0).toLocaleString('en') + ' versions. Writing the claims…'; render(); });
  es.addEventListener('claims', e => { S.claims = JSON.parse(e.data).claims;
    setStep('st-claims', 'done'); stepText('st-grid', null, 'run'); $('strips').classList.remove('pending');
    $('status').textContent = S.claims.length + ' claims. Reading old versions against them…'; render(); });
  es.addEventListener('probes', e => { const d = JSON.parse(e.data); d.items.forEach(addProbe); S.rounds.push(d.round);
    if (d.round === 0) { setStep('st-grid', 'done'); stepText('st-bisect', null, 'run'); }
    $('status').textContent = d.round ? 'Round ' + d.round + ': ' + S.changes.length + ' change' + (S.changes.length === 1 ? '' : 's') + ' found so far, narrowing down the edits…'
                                      : 'First pass done: the chart is rough, sharpening it now…';
    render(); });
  es.addEventListener('change', e => { const c = JSON.parse(e.data); S.changes.push(c); renderHeadlines();
    if (S.rounds.length) { const li = $('st-bisect'); li.textContent = li.textContent.replace(/: .*/, '') +
      ': ' + S.changes.length + ' found'; } render(); });
  es.addEventListener('summary', e => { const d = JSON.parse(e.data); S.summary = d.text; if (!d.text) return;
    $('summary').innerHTML = esc(d.text).split(/\n+/).filter(Boolean).map(p => '<p>' + p.replace(/^(ANSWER:|DRIFT:|RÉPONSE:|DÉRIVE:|SVAR:|DRIFT:|VASTAUS:|MUUTOS:)/, '<b>$1</b>') + '</p>').join('') +
      '<span class="ai">Written by Gemini from the articles and the changes below. Check the sources.</span>';
    $('summary').hidden = false; });
  es.addEventListener('done', e => { S.done = JSON.parse(e.data); stepText('st-bisect',
    'Narrowing down the edits: ' + S.changes.length + ' found, each on an exact edit', 'done'); finishRun(); });
  es.addEventListener('problem', e => { $('err').textContent = JSON.parse(e.data); $('err').hidden = false;
    const cur = document.querySelector('.steps li.run'); if (cur) cur.className = 'err'; finishRun(); });
  es.onerror = () => { if (S.running) { $('err').textContent = 'The connection to the server was lost. Run the trace again.'; $('err').hidden = false; finishRun(); } };
}
function finishRun() { S.running = false; $('go').disabled = false; clearInterval(tickTimer); if (es) es.close();
  $('loadbar').classList.remove('on'); $('strips').classList.remove('pending'); renderHeadlines();
  if (S.done) { $('status').innerHTML = '<b>' + S.changes.length + ' change' + (S.changes.length === 1 ? '' : 's') + ' of position</b> in ' +
    ((Date.now() - tStart) / 1000).toFixed(1) + ' s. ' + S.done.requests + ' requests to the Gemini Decisions API' +
    (S.done.cache_hits ? ', ' + S.done.cache_hits + ' answers reused from earlier runs' : '') + '.';
    $('steps').hidden = true; } }
// One colour per stance, the same as the chart, whichever side of "became" it is on.
const STANCE_CLASS = { 'stated as fact': 'st-fact', 'stated with attribution': 'st-attr', 'not addressed': 'st-none',
  'presented as disputed': 'st-disp', 'denied': 'st-deny' };
const stanceChip = w => '<span class="stance ' + (STANCE_CLASS[String(w).trim().toLowerCase()] || 'st-none') + '">' + esc(w) + '</span>';
// "Claim label: stated as fact until February 2018, not addressed since"
function splitHeadline(h) {
  const m = String(h || '').match(/^(.*?):\s*(.+?) until (\w+ \d{4}),\s*(.+?) since$/);
  return m ? { claim: m[1], was: m[2], when: m[3], now: m[4] } : null;
}
function renderHeadlines() {
  const ul = $('headlines'); ul.innerHTML = '';
  $('chghead').hidden = !S.changes.length && !S.done;
  S.changes.map((c, i) => [c, i]).sort((x, y) => x[0].t < y[0].t ? 1 : -1).forEach(([c, i]) => {
    const h = splitHeadline(c.headline);
    const label = h ? h.claim : ((S.claims.find(x => x.id === c.claim) || {}).label || c.claim);
    const art = S.articles[c.a] ? S.articles[c.a].title : '';
    const li = document.createElement('li'); if (S.selChange === i) li.className = 'sel';
    li.innerHTML = '<span class="when">' + esc(h ? h.when : c.t.slice(0, 7)) + (S.articles.length > 1 ? ', ' + esc(art) : '') + '</span>' +
      '<a href="#" class="claim move">' + esc(label) + '</a>' +
      (h ? '<span class="move">' + stanceChip(h.was) + ' became ' + stanceChip(h.now) + '</span>'
         : '<span class="move">' + esc(c.headline) + '</span>') +
      (c.comment ? '<span class="edit-note">Edit summary: “' + esc(c.comment) + '”</span>' : '');
    li.querySelector('a').onclick = ev => { ev.preventDefault(); showChange(S.changes[i], i); };
    ul.appendChild(li); });
  if (!S.changes.length && S.done) ul.innerHTML = '<li class="empty muted">The articles kept the same position on every claim.</li>';
}

let raf = 0;
function render() { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; drawStrips(); drawSparks(); }); }

const W = 1000, LBL = 330;
let T0 = Date.UTC(Y0, 0), T1 = Date.UTC(Y1, 0);
// the time axis follows the articles: from the first year of the oldest history to the year after the newest edit
function setAxis(arts) {
  if (!arts.length) return;
  const first = Math.min(...arts.map(a => new Date(a.first).getUTCFullYear()));
  const last = Math.max(...arts.map(a => new Date(a.last).getUTCFullYear()));
  Y0 = first; Y1 = Math.max(last + 1, first + 2); T0 = Date.UTC(Y0, 0); T1 = Date.UTC(Y1, 0);
}
const xOf = t => LBL + (tms(t) - T0) / (T1 - T0) * (W - LBL);
const yearOf = t => (new Date(t)).getUTCFullYear() + (new Date(t)).getUTCMonth() / 12;

function drawStrips() {
  if (!S.claims.length) { $('strips').innerHTML = ''; return; }
  const order = rowOrder(), H = 26 + order.length * ROW_H();
  const ns = 'http://www.w3.org/2000/svg';
  let html = '<svg id="svg" viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;display:block" role="img" ' +
    'aria-label="stance strips: rows are claims, lanes are articles, colors show position over time">';
  html += '<defs><pattern id="war" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">' +
          '<line x1="0" y1="0" x2="0" y2="6" stroke="var(--line)" stroke-width="2"/></pattern></defs>';
  const step = Y1 - Y0 > 30 ? 5 : Y1 - Y0 > 14 ? 2 : 1;
  for (let y = Math.ceil(Y0 / step) * step; y <= Y1; y += step) {
    const x = LBL + (Date.UTC(y) - T0) / (T1 - T0) * (W - LBL);
    if (x < LBL || x > W - 4) continue;
    html += '<line x1="' + x + '" y1="18" x2="' + x + '" y2="' + H + '" stroke="var(--soft)"/>' +
            '<text x="' + x + '" y="12" text-anchor="middle">' + y + '</text>';
  }
  order.forEach((c, ri) => {
    const top = 20 + ri * ROW_H();
    if (ri % 2 === 0) html += '<rect x="0" y="' + top + '" width="' + W + '" height="' + ROW_H() +
                              '" fill="var(--soft)" opacity="0.4"/>';
    const lab = wrap(c.label, 40, 2);       // the full sentence is not repeated under the label: tooltip and Proof panel carry it
    html += '<text class="claimlab" data-c="' + c.id + '" x="6" y="' + (top + 17) + '" font-size="12.5" font-weight="600" fill="var(--fg)" style="cursor:pointer">' +
            '<title>' + esc(c.text) + '</title>' +
            lab.map((l, i) => '<tspan x="6" dy="' + (i ? 14 : 0) + '">' + esc(l) + '</tspan>').join('') + '</text>';
    const laneTop = top + ROW_H() - Lanes() * LANE_H - 6;
    S.articles.forEach((a, ai) => {
      const y = laneTop + 2 + ai * LANE_H, list = (S.probes[ai] || {})[c.id] || [];
      html += '<text x="' + (LBL - 6) + '" y="' + (y + LANE_H - 2) + '" font-size="8" text-anchor="end"><title>' + esc(a.title) + '</title>' + (ai + 1) + '</text>';
      html += '<rect x="' + LBL + '" y="' + (y + (LANE_H - 2) / 2) + '" width="' + (W - LBL) +
              '" height="1" fill="var(--muted)" opacity="0.22"/>';
      if (!list.length) return;
      let from = LBL, val = list[0];
      const paint = (x1, x2, v) => { const b = bucket(v[1], v[2]); if (b === 0) return;
        html += '<rect class="lane" data-a="' + ai + '" data-c="' + c.id + '" data-rev="' + v[3] + '" x="' + x1 + '" y="' + y +
                '" width="' + Math.max(0.7, x2 - x1) + '" height="' + (LANE_H - 2) + '" fill="' + COL[b] +
                '" opacity="' + (0.25 + 0.75 * v[2]).toFixed(2) + '"><title>' + esc(a.title) + ' · ' + esc(c.label) +
                ' — click for the evidence paragraph</title></rect>'; };
      list.forEach(v => { paint(from, xOf(new Date(v[0]).toISOString()), v); from = xOf(new Date(v[0]).toISOString()); val = v; });
      paint(from, W, val);
      (a.wars || []).forEach(w => { const x1 = Math.max(LBL, xOf(w[0])), x2 = Math.min(W, xOf(w[1]));
        html += '<rect x="' + x1 + '" y="' + y + '" width="' + Math.max(1, x2 - x1) + '" height="' + (LANE_H - 2) +
                '" fill="url(#war)" opacity="0.55" pointer-events="none"/>'; });
    });
    html += '<line x1="0" y1="' + (top + ROW_H() - 3) + '" x2="' + W + '" y2="' + (top + ROW_H() - 3) +
            '" stroke="var(--line)" stroke-width="1" opacity="0.7"/>';
  });
  S.changes.forEach((c, i) => {
    const ri = order.findIndex(x => x.id === c.claim); if (ri < 0) return;
    const top = 20 + ri * ROW_H(), x = xOf(c.t);
    const laneTop = top + ROW_H() - Lanes() * LANE_H - 6, y = laneTop + 2 + c.a * LANE_H;
    html += '<line class="tick" data-i="' + i + '" x1="' + x + '" y1="' + (laneTop - 2) + '" x2="' + x +
            '" y2="' + (laneTop + Lanes() * LANE_H + 2) + '" stroke="var(--fg)" stroke-width="1.4" opacity="0.8"/>' +
            '<circle class="tick" data-i="' + i + '" tabindex="0" role="button" aria-label="' + esc(S.articles[c.a].title + ': ' + c.headline) +
            '" cx="' + x + '" cy="' + (y + (LANE_H - 2) / 2) + '" r="' + (S.selChange === i ? 7 : 4.5) + '" fill="var(--accent)" stroke="var(--bg)">' +
            '<title>' + esc(S.articles[c.a].title + ' — ' + c.headline) + '</title></circle>';
  });
  const cx = S.cursorX || LBL + 0.62 * (W - LBL);
  html += '<line class="cursorline" id="cur" x1="' + cx + '" y1="16" x2="' + cx + '" y2="' + H + '"/>' +
          '<line x1="' + (cx - 6) + '" y1="16" x2="' + (cx + 6) + '" y2="16" stroke="var(--accent)" stroke-width="3"/>' +
          '<text x="' + cx + '" y="' + (H - 4) + '" text-anchor="middle" fill="var(--accent)">' + cursorDate(cx) + '</text></svg>';
  $('strips').innerHTML = html;
  const svg = $('svg');
  svg.querySelectorAll('.tick').forEach(el => {
    el.addEventListener('click', ev => { ev.stopPropagation(); showChange(S.changes[+el.dataset.i], +el.dataset.i); });
    el.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); showChange(S.changes[+el.dataset.i], +el.dataset.i); } }); });
  svg.querySelectorAll('.claimlab').forEach(el => el.addEventListener('click', ev => { ev.stopPropagation();
    const cl = S.claims.find(x => x.id === el.dataset.c); if (!cl) return;
    $('proof').innerHTML = '<b>' + esc(cl.label) + '</b><div class="quote">' + esc(cl.text) + '</div>' +
      (cl.theme ? '<p class="meta">Theme: ' + esc(cl.theme) + '</p>' : ''); }));
  svg.querySelectorAll('.lane').forEach(el => el.addEventListener('click', ev => { ev.stopPropagation();
    showEvidence(+el.dataset.a, el.dataset.c, +el.dataset.rev); }));
  dragCursor(svg);
}
function cursorDate(cx) { const t = T0 + (cx - LBL) / (W - LBL) * (T1 - T0); return new Date(t).toISOString().slice(0, 10); }
function dragCursor(svg) {
  const line = $('cur'); if (!line) return;
  const move = ev => { const r = svg.getBoundingClientRect();
    const x = Math.min(W - 2, Math.max(LBL, (ev.clientX - r.left) / r.width * W));
    S.cursorX = x; line.setAttribute('x1', x); line.setAttribute('x2', x);
    line.nextElementSibling && (line.nextElementSibling.setAttribute('x', x), line.nextElementSibling.textContent = cursorDate(x));
    clearTimeout(move.tm); move.tm = setTimeout(() => loadAsof(cursorDate(x)), 250); };
  line.addEventListener('pointerdown', ev => { line.setPointerCapture(ev.pointerId); move(ev);
    const up = e => { line.releasePointerCapture(e.pointerId); line.removeEventListener('pointermove', move); };
    line.addEventListener('pointermove', move, { passive: true }); line.addEventListener('pointerup', up, { once: true }); });
}
function loadAsof(date) {
  const sel = S.selArticle != null ? S.articles[S.selArticle] : S.articles[0];
  if (!sel) return;
  const box = $('proof');
  box.innerHTML = '<p class="muted">Loading ' + esc(sel.title) + ' as of ' + esc(date) + '…</p>';
  fetch('/api/drift/asof?page_id=' + sel.page_id + '&date=' + date)
    .then(r => r.json()).then(d => {
      if (d.error) { box.innerHTML = '<p class="err">' + esc(d.error) + '</p>'; return; }
      if (d.rev_id === null) {
        box.innerHTML = '<b>' + esc(sel.title) + '</b> — ' + esc(date) +
          '<pre>' + esc(d.sql) + '</pre><p class="muted">The article did not exist yet on that date.</p>';
        return;
      }
      box.innerHTML = '<b>' + esc(sel.title) + '</b> — ' + esc(date) + ' (rev ' + esc(d.rev_id) + ')<br>' +
        '<a href="https://fi.wikipedia.org/w/index.php?oldid=' + d.rev_id + '" target="_blank" rel="noopener">This version on Wikipedia</a>' +
        '<pre>' + esc(d.sql) + '</pre><div id="asoftext">' + esc(d.text) + '</div>';
    });
}
function showChange(c, idx) {
  if (!c) return;
  if (idx !== undefined) { S.selChange = idx; render(); renderHeadlines(); }
  const a = S.articles[c.a], cl = S.claims.find(x => x.id === c.claim) || { label: c.claim, text: '' };
  const w = v => (v[0] >= 0 ? '+' : '') + Number(v[0]).toFixed(2) + ' / ' + Number(v[1]).toFixed(2);
  $('proof').innerHTML =
    '<b>' + esc(cl.label) + '</b><div class="muted" style="margin-bottom:6px">' + esc(cl.text) + '</div>' +
    '<div class="kv"><b>Article</b><span>' + esc(a.title) + '</span><b>Date</b><span>' + esc(c.t.slice(0, 10)) + '</span><b>Editor</b><span>' + esc(c.user) + '</span>' +
    '<b>Edit summary</b><span>' + esc(c.comment || 'none') + '</span><b>Stance, presence</b><span>' + esc(w(c.from)) + ' became ' + esc(w(c.to)) + '</span></div>' +
    (c.hunk.del.length ? '<p style="margin:10px 0 0">What the edit removed</p><div class="quote del">' + c.hunk.del.map(esc).join('<br>') + '</div>' : '') +
    (c.hunk.add.length ? '<p style="margin:10px 0 0">What the edit added</p><div class="quote add">' + c.hunk.add.map(esc).join('<br>') + '</div>' : '') +
    '<p><a href="https://fi.wikipedia.org/w/index.php?diff=' + c.rev_after + '&oldid=' + c.rev_before + '" target="_blank" rel="noopener">See the edit on Wikipedia</a>' +
    '<button type="button" class="quiet" id="evlink" style="margin-left:16px">Find the paragraph that states the claim</button></p>' +
    '<div id="evout" class="muted"></div>';
  $('evlink').onclick = ev => { ev.preventDefault();
    $('evout').textContent = 'Searching the text tree…';
    fetch('/api/drift/evidence?page_id=' + a.page_id + '&rev_id=' + c.rev_after + '&claim=' + encodeURIComponent(cl.text))
      .then(r => r.json()).then(d => {
        $('evout').innerHTML = d.error ? '<span class="err">' + esc(d.error) + '</span>' :
          '<div class="quote">' + esc(d.paragraph) + '</div><span class="muted">Found with ' + d.questions + ' yes or no questions.</span>'; });
  };
}
function showEvidence(ai, cid, revId) {
  const a = S.articles[ai], cl = S.claims.find(x => x.id === cid);
  if (!a || !cl) return;
  $('proof').innerHTML = '<b>' + esc(a.title) + ' · ' + esc(cl.label) + '</b><p class="muted">Searching the text tree for the paragraph…</p>';
  fetch('/api/drift/evidence?page_id=' + a.page_id + '&rev_id=' + revId + '&claim=' + encodeURIComponent(cl.text))
    .then(r => r.json()).then(d => {
      $('proof').innerHTML = '<b>' + esc(a.title) + ' · ' + esc(cl.label) + '</b><div class="quote">' +
        (d.error ? '<span class="err">' + esc(d.error) + '</span>' : esc(d.paragraph || '(nothing found at this version)')) +
        '</div>' + (d.paragraph ? '' : '<p class="muted">' + esc(d.note || 'This version was probed by the grid; the evidence search runs live and costs a few Decisions requests.') + '</p>');
    });
}
function drawSparks() {
  const wrap = $('sparkwrap');
  wrap.innerHTML = S.articles.map((a, ai) => {
    const act = a.activity || [], n = Math.max(1, act.length), hi = Math.max(1, ...act.map(r => r[1]));
    const pts = act.map((r, i) => (i / n * 160).toFixed(1) + ',' + (26 - r[1] / hi * 24).toFixed(1)).join(' ');
    const bars = act.map((r, i) => r[2] ? '<rect x="' + (i / n * 160).toFixed(1) + '" y="2" width="1.6" height="24" fill="var(--red)" opacity=".7"/>' : '').join('');
    return '<div class="spark' + (S.selArticle === ai ? ' sel' : '') + '" data-i="' + ai + '" title="' + esc(a.title) +
      ': ' + a.n_revs + ' versions, ' + (a.wars || []).length + ' edit wars (red marks are reverts). Choose it to read it at the blue line."><b>' + (ai + 1) + '. ' + esc(a.title) +
      '</b><svg width="164" height="28" viewBox="0 0 164 28">' + bars +
      '<polyline points="' + pts + '" fill="none" stroke="var(--link)" stroke-width="1.2"/></svg></div>';
  }).join('');
  wrap.querySelectorAll('.spark').forEach(el => el.addEventListener('click', () => {
    S.selArticle = +el.dataset.i; drawSparks();
    loadAsof(S.cursorX ? cursorDate(S.cursorX) : '2010-06-01');
  }));
}
$('f').addEventListener('submit', ev => { ev.preventDefault();
  const q = $('q').value.trim(); if (!q) return;
  history.replaceState(null, '', '/drift?q=' + encodeURIComponent(q));
  start(q, false); });
$('onlychg').addEventListener('change', render);
$('dl').addEventListener('click', () => {
  const data = { question: S.q, articles: S.articles.map(a => ({ title: a.title, page_id: a.page_id, n_revs: a.n_revs, first: a.first, last: a.last, wars: a.wars })),
    claims: S.claims, probes: S.probes, changes: S.changes, summary: S.summary };
  const a = document.createElement('a'); a.download = 'opinion-drift.json';
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' })); a.click(); URL.revokeObjectURL(a.href); });
$('cp').addEventListener('click', async () => { try { await navigator.clipboard.writeText(location.href); $('cp').textContent = 'Link copied'; }
  catch (e) { $('cp').textContent = 'Copy failed'; } setTimeout(() => $('cp').textContent = 'Copy link', 1800); });
const FAST = new URLSearchParams(location.search).has('fast');
const PARAM_FIXTURE = {{ 'true' if fixture else 'false' }};
const PARAM_Q = {{ q|tojson }};
const PARAM_LANG = {{ lang|tojson }};
document.querySelectorAll('#ex button').forEach(b => b.addEventListener('click', () => {
  const fx = !!b.dataset.fixture, q = b.dataset.q || '';
  $('q').value = q;
  history.replaceState(null, '', fx ? '/drift?fixture=1' : '/drift?q=' + encodeURIComponent(q));
  start(q, fx); }));
if (PARAM_FIXTURE || PARAM_Q) start(PARAM_Q, PARAM_FIXTURE);
</script>
</body></html>"""

_TEMPLATE = Environment(autoescape=True).from_string(PAGE)
