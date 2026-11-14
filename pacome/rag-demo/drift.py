"""Opinion drift (/drift): how the top articles for a question changed their stance on shared claims over time.

  GET /drift                    the page
  GET /api/drift/stream?q=...   SSE; q is a question, or an exact article title (then &lang= may name its edition);
                                with fixture=1 replays drift_fixture.json, else runs stance.run(q) in a thread
                                with keep-alive comments every HEARTBEAT seconds
  GET /api/drift/asof           article text at a date (history.text_asof), with the SQL shown
  GET /api/drift/evidence       the paragraph carrying a claim at a version (stance.evidence)

Events follow the shared contract: sources, claims, probes, change, done | problem.
"""
import functools
import json
import queue
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template_string, request

import history
import hub
import stance

bp = Blueprint("drift", __name__)
FIXTURE_PATH = Path(__file__).parent / "drift_fixture.json"
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
    for i, (event, data) in enumerate(fixture):
        if event == "probes":
            yield sse(event, data)
            time.sleep(0 if fast else 2.2 if data["round"] == 0 else 1.2)
        else:
            yield sse(event, data)
            time.sleep(0 if fast else 0.25)


def _pump(question, out, stop, lang=None):
    """Run the engine in a thread so the HTTP stream can send keep-alives while a slow step (Gemini, history load) runs."""
    try:
        for event, data in stance.run(question, lang):
            if stop.is_set():       # the reader went away: stop at the next event instead of scoring the rest
                return
            out.put(sse(event, data))
    except Exception as e:
        out.put(sse("problem", f"{type(e).__name__}: {e}"))
    finally:
        out.put(None)


@bp.route("/api/drift/stream")
def stream():
    q = request.args.get("q", "").strip()
    fixture = request.args.get("fixture")
    fast = bool(request.args.get("fast"))     # read here: the request context is gone once the stream starts
    lang = request.args.get("lang", "").strip().lower()   # the extension sends the edition of the article in the tab
    lang = lang if len(lang) == 2 and lang.isalpha() else None

    def events():
        try:
            if fixture:
                yield from _fixture_events(fast)
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

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/api/drift/asof")
def asof():
    try:
        page_id = int(request.args.get("page_id", 0))
        rev_id, text, sql = history.text_asof(page_id, request.args.get("date", ""))
        return jsonify(rev_id=rev_id, text=text, sql=sql)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@functools.lru_cache(maxsize=256)
def _evidence(page_id, rev_id, claim):
    """Each lookup costs several Decisions requests: remember the answer for the life of the server."""
    return stance.evidence(page_id, rev_id, claim)


@bp.route("/api/drift/evidence")
def evidence():
    try:
        claim = request.args.get("claim", "").strip()
        if not claim:
            return jsonify(error="missing claim"), 400
        return jsonify(_evidence(int(request.args.get("page_id", 0)), int(request.args.get("rev_id", 0)), claim))
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@bp.route("/drift")
def page():
    q = request.args.get("q", "").strip()
    fixture = bool(request.args.get("fixture"))
    return render_template_string(PAGE, nav=hub.BANNER, q=q, fixture=fixture, examples=EXAMPLES)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opinion drift · MariaDB time travel</title>
<style>
:root { --bg:#fff; --fg:#202122; --muted:#54595d; --line:#a2a9b1; --soft:#eaecf0; --card:#f8f9fa;
        --link:#3366cc; --accent:#36c; --code:#f8f9fa; --red:#d33; --green:#14866d;
        --fact:#1f5fbf; --attr:#9dc3ea; --disp:#f4a35f; --deny:#d1490f; --serif:Georgia,Times,serif;
        --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }
@media (prefers-color-scheme: dark) {
:root { --bg:#101418; --fg:#eaecf0; --muted:#a2a9b1; --line:#54595d; --soft:#27292d; --card:#1b1f23;
        --link:#88a3e8; --accent:#6485d1; --code:#1b1f23; --red:#ff4242; --green:#2ec4a0;
        --fact:#5b8fd6; --attr:#3d5f8f; --disp:#9a5c2e; --deny:#d1490f; } }
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.6 var(--sans) }
a { color:var(--link); text-decoration:none } a:hover { text-decoration:underline }
main { max-width:1400px; margin:0 auto; padding:20px clamp(16px,3vw,48px) 64px }
h1 { font:400 1.8rem/1.3 var(--serif); margin:0 0 4px; padding-bottom:2px; border-bottom:1px solid var(--line) }
.sub { color:var(--muted); margin:6px 0 14px; font-size:.9rem }
form { display:flex; flex-wrap:wrap; gap:0; max-width:900px }
input[type=text] { flex:1 1 320px; padding:7px 12px; font:inherit; font-size:1rem; color:inherit; background:var(--bg);
                   border:1px solid var(--line); border-right:0; border-radius:2px 0 0 2px }
input[type=text]:focus { outline:0; border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent) }
button { padding:7px 18px; font:inherit; font-weight:700; color:#fff; background:var(--accent); border:1px solid var(--accent);
         border-radius:0 2px 2px 0; cursor:pointer } button:disabled { opacity:.6; cursor:wait }
.grid { display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:24px; align-items:start; margin-top:10px }
@media (max-width:900px) { .grid { grid-template-columns:1fr } aside.panel { position:static !important } }
aside.panel { position:sticky; top:12px; max-height:calc(100vh - 24px); overflow-y:auto; background:var(--card);
              border:1px solid var(--line); font-size:.88rem }
aside.panel h2 { margin:0; padding:7px 10px; text-align:center; background:var(--soft); font:700 .95rem var(--sans);
                 border-bottom:1px solid var(--line) }
.panel .pad { padding:10px 12px }
.status { font-size:.9rem; color:var(--muted); margin:10px 0 } .status b { color:var(--fg) }
.err { color:var(--red); border:1px solid var(--red); background:rgba(221,51,51,.06); padding:10px 12px; margin:12px 0 }
#headlines { margin:12px 0; padding:0; list-style:none; font-size:.92rem }
#headlines li { padding:3px 0 3px 18px; position:relative }
#headlines li::before { content:'\25B6'; position:absolute; left:0; color:var(--accent); font-size:.7em; top:7px }
#sparkwrap { display:flex; flex-wrap:wrap; gap:14px; margin:12px 0 6px }
.spark { border:1px solid var(--line); background:var(--card); padding:6px 8px; font-size:.75rem; cursor:pointer }
.spark.sel { outline:2px solid var(--accent) } .spark b { display:block; font-weight:600; margin-bottom:2px }
#strips { border:1px solid var(--line); background:var(--card); position:relative }
svg text { font:11px var(--sans); fill:var(--muted) }
.lane { cursor:pointer } .tick { cursor:pointer }
.legend { display:flex; gap:16px; flex-wrap:wrap; font-size:.8rem; color:var(--muted); margin:8px 2px }
.legend i { display:inline-block; width:22px; height:11px; margin-right:5px; vertical-align:-1px; border:1px solid var(--line) }
pre { background:var(--code); border:1px solid var(--soft); padding:8px; overflow-x:auto; font-size:.75rem; line-height:1.4 }
.quote { border-left:3px solid var(--accent); margin:6px 0; padding:4px 10px; background:var(--soft); font-size:.85rem }
.del { color:var(--red); text-decoration:line-through } .add { color:var(--green) }
.kv { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; font-size:.85rem } .kv b { font-weight:600 }
.cursorline { stroke:var(--accent); stroke-width:1.5; cursor:ew-resize }
#asoftext { max-height:340px; overflow-y:auto; white-space:pre-wrap; font-size:.8rem; line-height:1.5 }
.steps { list-style:none; padding:10px 12px; margin:16px 0 0; font-size:.9rem; background:var(--card);
         border:1px solid var(--line); border-left:4px solid var(--accent) }
.steps li { padding:2px 0; color:var(--muted) } .steps li.done { color:var(--fg) }
.steps li::before { content:'\25CB'; display:inline-block; width:1.5em }
.steps li.done::before { content:'\2713'; color:var(--green); font-weight:700 }
.steps li.err { color:var(--red) } .steps li.err::before { content:'\2717'; display:inline-block; width:1.5em; font-weight:700 }
.steps li.run::before { content:''; width:.8em; height:.8em; margin-right:.7em; vertical-align:-1px; display:inline-block;
                        border:2px solid var(--line); border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite }
.steps li.run[data-t]::after { content:' ' attr(data-t) ' s'; color:var(--accent); font-variant-numeric:tabular-nums }
@keyframes spin { to { transform:rotate(360deg) } }
#loadbar { height:3px; background:var(--soft); overflow:hidden; position:relative; margin:2px 0 10px; display:none }
#loadbar.on { display:block }
#loadbar i { position:absolute; left:0; top:0; height:3px; width:38%; background:var(--accent); animation:slide 1.15s ease-in-out infinite }
@keyframes slide { 0% { transform:translateX(-100%) } 100% { transform:translateX(320%) } }
#strips.pending { min-height:200px; background:
   repeating-linear-gradient(0deg,var(--card) 0 108px,var(--line) 108px 110px) }
.srcwrap { font-size:.88rem; margin:14px 0 4px; background:var(--card); border:1px solid var(--line); padding:8px 12px }
ol.src { padding:0; margin:4px 0 0; list-style:none; counter-reset:s }
ol.src li { counter-increment:s; padding:2px 0 2px 24px; position:relative }
ol.src li::before { content:counter(s); position:absolute; left:2px; top:2px; color:var(--muted); font-weight:700 }
.badge { display:inline-block; font-size:.68rem; text-transform:uppercase; padding:0 5px; border:1px solid var(--line);
         border-radius:2px; margin-right:6px; color:var(--muted); background:var(--bg) }
.meta { color:var(--muted); font-size:.8rem }
[hidden] { display:none !important }
.summary { border:1px solid var(--line); border-left:4px solid var(--accent); background:var(--card); padding:10px 14px; margin:12px 0; font-size:.95rem; max-width:900px }
.summary p { margin:4px 0 } .summary .ai { display:block; margin-top:6px; font-size:.75rem; color:var(--muted) }
.ex { margin:10px 0 0; font-size:.85rem; color:var(--muted) } .ex a { margin-right:14px; white-space:nowrap }
.tools { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin:10px 0 4px; font-size:.85rem; color:var(--muted) }
.tools button { padding:3px 10px; font-weight:400; color:var(--fg); background:var(--card); border:1px solid var(--line); border-radius:2px }
.tools label { cursor:pointer }
#strips { overflow-x:auto } #strips svg { min-width:760px }
.tick:focus { outline:none } circle.tick:focus-visible { stroke:var(--fg); stroke-width:2 }
#headlines .empty { color:var(--muted) } #headlines li.sel a { font-weight:700 }
footer { margin-top:36px; padding-top:12px; border-top:1px solid var(--line); color:var(--muted); font-size:.8rem }
</style></head>
<body>
{{ nav|safe }}
<main>
<h1>Opinion drift</h1>
<p class="sub">What the five closest Finnish Wikipedia articles <em>said then and say now</em> about your question.
Every step in the ribbons below sits on a real edit found by binary search over the MariaDB edit log.</p>
<form id="f"><input type="text" id="q" placeholder="Ask in English, Finnish or Swedish" value="{{ q }}" autofocus>
<button id="go" type="submit">Trace drift</button></form>
<p class="ex" id="ex">Try: {% for e in examples %}<a href="/drift?q={{ e|urlencode }}">{{ e }}</a>{% endfor %}<a href="/drift?fixture=1">replay the demo</a></p>
<div class="tools" id="tools" hidden>
  <label><input type="checkbox" id="onlychg"> only claims with a change point</label>
  <button type="button" id="dl">Download results (JSON)</button><button type="button" id="cp">Copy link</button>
</div>
<ul class="steps" id="steps" hidden>
  <li id="st-load">Reading Finnish edit histories (lazy load, ~4 s per article)</li>
  <li id="st-claims">Generating the shared claims (Gemini)</li>
  <li id="st-grid">Grid round: scoring 9 versions per article against every claim (~45 requests)</li>
  <li id="st-bisect">Bisecting change points (up to 14 rounds)</li>
</ul>
<p class="status" id="status" hidden></p>
<div id="loadbar"><i></i></div>
<div class="err" id="err" hidden></div>
<div class="srcwrap" id="srcwrap" hidden><b>Sources</b> — the five Finnish articles behind the strips:<ol id="srclist" class="src"></ol></div>
<div class="summary" id="summary" hidden></div>
<ul id="headlines"></ul>
<div class="grid">
<section>
  <div id="sparkwrap"></div>
  <div id="strips"></div>
  <div class="legend">
    <span><i style="background:var(--fact)"></i>stated as fact</span>
    <span><i style="background:var(--attr)"></i>attributed / hedged</span>
    <span><i style="background:transparent"></i>not addressed</span>
    <span><i style="background:var(--disp)"></i>presented as disputed</span>
    <span><i style="background:var(--deny)"></i>denied</span>
    <span><i style="background:repeating-linear-gradient(45deg,var(--line) 0 2px,transparent 2px 6px)"></i>edit war</span>
    <span>opacity = how present the claim is; <b>|</b> = the exact change-point edit</span>
  </div>
</section>
<aside class="panel"><h2>Proof</h2><div class="pad" id="proof">
<p class="muted">Click a tick for the edit that changed a position, a ribbon cell for the evidence paragraph,
or drag the vertical cursor to read an article as it stood that day.</p></div></aside>
</div>
<footer>Text: Wikipedia, CC BY-SA 4.0 · Stances scored by the Gemini Decisions API against the same claims for every
version · Boundaries located by binary search over MariaDB system-versioned history · Positions and stances are
machine-judged; check the linked diffs.<br>
<b>Known limits:</b> Finnish history only · a topic with no Finnish article is dropped from the five ·
snapshots are cut at 16,000 cleaned characters · a claim that appears and disappears between two grid probes
outside an edit war can be missed.</footer>
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
const ROW_H = () => Lanes() * LANE_H + 20;
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
  es = new EventSource('/api/drift/stream?fixture=' + (fixture ? '1' : '') + (FAST ? '&fast=1' : '') + '&q=' + encodeURIComponent(q || ''));
  es.addEventListener('sources', e => { const d = JSON.parse(e.data); S.articles = d.articles; setAxis(d.articles); $('tools').hidden = false;
    setStep('st-load', 'done'); stepText('st-claims', null, 'run');
    $('srclist').innerHTML = d.articles.map(a => '<li id="src' + a.i + '"><span class="badge">fi</span><a href="' + esc(a.url) +
      '" target="_blank" rel="noopener">' + esc(a.title) + '</a> <span class="meta">' + a.n_revs.toLocaleString() +
      ' versions, ' + esc(String(a.first).slice(0, 10)) + ' → ' + esc(String(a.last).slice(0, 10)) +
      (a.distance !== undefined ? ' · distance to your question ' + a.distance.toFixed(2) : '') + '</span></li>').join('') +
      ((d.timings && d.timings.dropped_far) ? '<li class="meta" style="list-style:none">' + d.timings.dropped_far + ' further article(s) left out: too far from your question.</li>' : '');
    $('srcwrap').hidden = false;
    $('status').textContent = d.articles.length + ' articles with edit history (' +
      d.articles.reduce((s, a) => s + a.n_revs, 0).toLocaleString() + ' versions). Generating claims…'; render(); });
  es.addEventListener('claims', e => { S.claims = JSON.parse(e.data).claims;
    setStep('st-claims', 'done'); stepText('st-grid', null, 'run'); $('strips').classList.remove('pending');
    $('status').textContent = S.claims.length + ' claims generated. Scoring the coarse grid…'; render(); });
  es.addEventListener('probes', e => { const d = JSON.parse(e.data); d.items.forEach(addProbe); S.rounds.push(d.round);
    if (d.round === 0) { setStep('st-grid', 'done'); stepText('st-bisect', null, 'run'); }
    $('status').textContent = 'Round ' + d.round + ': ' + d.items.length + ' versions scored' +
      (d.round ? ' — narrowing ' + S.changes.length + ' change points so far…' : ' — coarse ribbons shown, sharpening next…');
    render(); });
  es.addEventListener('change', e => { const c = JSON.parse(e.data); S.changes.push(c); renderHeadlines();
    if (S.rounds.length) { const li = $('st-bisect'); li.textContent = li.textContent.replace(/ — .*/, '') +
      ' — found ' + S.changes.length + ' change points'; } render(); });
  es.addEventListener('summary', e => { const d = JSON.parse(e.data); S.summary = d.text; if (!d.text) return;
    $('summary').innerHTML = esc(d.text).split(/\n+/).filter(Boolean).map(p => '<p>' + p.replace(/^(ANSWER:|DRIFT:|RÉPONSE:|DÉRIVE:|SVAR:|DRIFT:|VASTAUS:|MUUTOS:)/, '<b>$1</b>') + '</p>').join('') +
      '<span class="ai">Written by Gemini from the current article openings, the claim positions and the change points above; machine-generated, check the sources.</span>';
    $('summary').hidden = false; });
  es.addEventListener('done', e => { S.done = JSON.parse(e.data); stepText('st-bisect',
    'Bisection finished: ' + S.changes.length + ' change points located on exact edits', 'done'); finishRun(); });
  es.addEventListener('problem', e => { $('err').textContent = JSON.parse(e.data); $('err').hidden = false;
    const cur = document.querySelector('.steps li.run'); if (cur) cur.className = 'err'; finishRun(); });
  es.onerror = () => { if (S.running) { $('err').textContent = 'Connection to the server was lost.'; $('err').hidden = false; finishRun(); } };
}
function finishRun() { S.running = false; $('go').disabled = false; clearInterval(tickTimer); if (es) es.close();
  $('loadbar').classList.remove('on'); $('strips').classList.remove('pending'); renderHeadlines();
  if (S.done) $('status').innerHTML = '<b>Done in ' + ((Date.now() - tStart) / 1000).toFixed(1) + ' s:</b> ' + S.done.requests +
    ' Decisions requests, ' + (S.done.tokens/1e6).toFixed(2) + ' M tokens' +
    (S.done.cache_hits ? ' · ' + S.done.cache_hits + ' stances from cache (repeat runs are instant)' : '') + '.'; }
function renderHeadlines() {
  const ul = $('headlines'); ul.innerHTML = '';
  S.changes.map((c, i) => [c, i]).sort((x, y) => x[0].t < y[0].t ? -1 : 1).forEach(([c, i]) => {
    const li = document.createElement('li'); if (S.selChange === i) li.className = 'sel';
    li.innerHTML = '<a href="#" data-idx="' + i + '"><span class="meta">' + esc(c.t.slice(0, 7)) + ' · ' + esc(S.articles[c.a].title) + '</span> — ' + esc(c.headline) + '</a>';
    li.querySelector('a').onclick = ev => { ev.preventDefault(); showChange(S.changes[i], i); };
    ul.appendChild(li); });
  if (!S.changes.length && S.done) ul.innerHTML = '<li class="empty">No change point found: the five articles kept the same position on every claim.</li>';
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
      (cl.theme ? '<p class="meta">theme: ' + esc(cl.theme) + '</p>' : ''); }));
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
        '<a href="https://fi.wikipedia.org/?curid=' + sel.page_id + '" target="_blank" rel="noopener">fi.wikipedia.org</a>' +
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
    '<div class="kv"><b>Date</b><span>' + esc(c.t.slice(0, 10)) + '</span><b>Editor</b><span>' + esc(c.user) + '</span>' +
    '<b>Summary</b><span>' + esc(c.comment) + '</span><b>Stance</b><span>' + esc(w(c.from)) + ' → ' + esc(w(c.to)) + '</span></div>' +
    '<p style="margin:8px 0 4px"><b>Removed</b></p><div class="quote del">' + c.hunk.del.map(esc).join('<br>') + '</div>' +
    '<p style="margin:8px 0 4px"><b>Added</b></p><div class="quote add">' + c.hunk.add.map(esc).join('<br>') + '</div>' +
    '<p><a href="https://fi.wikipedia.org/?diff=' + c.rev_after + '" target="_blank" rel="noopener">open this diff on Wikipedia</a> · ' +
    '<a href="#" id="evlink">where does the claim sit?</a></p>' +
    '<div id="evout" class="muted"></div>';
  $('evlink').onclick = ev => { ev.preventDefault();
    $('evout').textContent = 'Searching the text tree…';
    fetch('/api/drift/evidence?page_id=' + a.page_id + '&rev_id=' + c.rev_after + '&claim=' + encodeURIComponent(cl.text))
      .then(r => r.json()).then(d => {
        $('evout').innerHTML = d.error ? '<span class="err">' + esc(d.error) + '</span>' :
          '<div class="quote">' + esc(d.paragraph) + '</div><span class="muted">' + d.questions + ' yes/no questions asked</span>'; });
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
      ': ' + a.n_revs + ' versions, ' + (a.wars || []).length + ' edit wars (red = reverts)"><b>' + esc(a.title) +
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
if (PARAM_FIXTURE || PARAM_Q) start(PARAM_Q, PARAM_FIXTURE);
else $('status').textContent = 'Ask a question, or add ?fixture=1 to watch the demo replay.';
</script>
</body></html>"""
