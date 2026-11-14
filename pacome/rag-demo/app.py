"""The demo page: ask a question, get a cited answer, see the SQL.   python app.py  ->  http://localhost:8000"""
import argparse
import json
import time

from flask import Flask, Response, request, render_template_string

import hub
import consistency
import drift
import gaps
import links
import refs
import mirror
import neighbors
import rag

app = Flask(__name__)
app.register_blueprint(hub.bp)
app.register_blueprint(drift.bp)
app.register_blueprint(mirror.bp)
app.register_blueprint(neighbors.bp)
app.register_blueprint(gaps.bp)
app.register_blueprint(refs.bp)
app.register_blueprint(links.bp)
app.register_blueprint(consistency.bp)

EXAMPLES = [
    "Which peace treaty ended the Winter War, and what did Finland lose?",
    "Kuka oli Suomen ensimmäinen presidentti ja milloin hänet valittiin?",
    "Vad hände med Åland efter första världskriget?",
    "Why did the Finnish Civil War start in 1918?",
]

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ask Wikipedia · MariaDB Vector</title>
<style>
  :root { --bg:#fff; --page:#fff; --fg:#202122; --muted:#54595d; --line:#a2a9b1; --soft:#eaecf0; --card:#f8f9fa;
          --link:#3366cc; --visited:#795cb2; --accent:#36c; --accent-fg:#fff; --code:#f8f9fa; --red:#d33; --green:#14866d;
          --serif:'Linux Libertine','Georgia','Times',serif; --sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Lato,Helvetica,Arial,sans-serif; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#101418; --page:#101418; --fg:#eaecf0; --muted:#a2a9b1; --line:#54595d; --soft:#27292d; --card:#1b1f23;
            --link:#88a3e8; --visited:#b6a0e0; --accent:#6485d1; --accent-fg:#101418; --code:#1b1f23; --red:#ff4242; --green:#2ec4a0; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.6 var(--sans); }
  a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
  header.top { border-bottom:1px solid var(--line); background:var(--page); }
  header.top .in { max-width:1400px; margin:0 auto; padding:10px clamp(16px,3vw,48px); display:flex; align-items:center; gap:14px; }
  .logo { width:44px; height:44px; flex:none; border-radius:50%; background:radial-gradient(circle at 50% 50%, var(--bg) 0 28%, transparent 29%),
          conic-gradient(var(--fg) 0 12%, var(--soft) 0 25%, var(--fg) 0 37%, var(--soft) 0 50%, var(--fg) 0 62%, var(--soft) 0 75%, var(--fg) 0 87%, var(--soft) 0); }
  .brand b { display:block; font:400 1.55rem/1.1 var(--serif); letter-spacing:.01em; }
  .brand span { font-size:.72rem; color:var(--muted); }
  main { max-width:1400px; margin:0 auto; padding:20px clamp(16px,3vw,48px) 64px; }
  h1 { font:400 1.8rem/1.3 var(--serif); margin:0 0 4px; padding-bottom:2px; border-bottom:1px solid var(--line); }
  .sub { color:var(--muted); margin:6px 0 18px; font-size:.9rem; }
  form { display:flex; gap:0; flex-wrap:wrap; max-width:900px; }
  input[type=text] { flex:1 1 320px; padding:7px 12px; font:inherit; font-size:1rem; color:inherit; background:var(--bg);
                     border:1px solid var(--line); border-right:0; border-radius:2px 0 0 2px; }
  input[type=text]:focus, select:focus { outline:0; border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); }
  select { padding:7px 10px; font:inherit; color:inherit; background:var(--card); border:1px solid var(--line); border-right:0; }
  button { padding:7px 18px; font:inherit; font-weight:700; color:var(--accent-fg); background:var(--accent); border:1px solid var(--accent);
           border-radius:0 2px 2px 0; cursor:pointer; } button:hover { filter:brightness(1.1); } button:disabled { opacity:.6; cursor:wait; }
  .ex { margin:10px 0 0; font-size:.85rem; color:var(--muted); } .ex a { margin-right:14px; white-space:nowrap; }
  .grid { display:grid; grid-template-columns:minmax(0,1fr) 320px; gap:32px; align-items:start; margin-top:8px; }
  .grid > aside { position:sticky; top:16px; max-height:calc(100vh - 32px); overflow-y:auto; background:var(--card); border:1px solid var(--line); padding:0; font-size:.88rem; }
  aside h2 { margin:0; padding:7px 10px; text-align:center; background:var(--soft); font:700 .95rem var(--sans); border-bottom:1px solid var(--line); }
  @media (max-width:900px) { .grid { grid-template-columns:1fr; } .grid > aside { position:static; max-height:none; } }
  .answer { font-size:1rem; line-height:1.7; margin:18px 0 8px; }
  .answer p { margin:.5em 0 .8em; } .answer ul { margin:.3em 0 .8em 1.6em; padding:0; } .answer li { margin-bottom:.25em; }
  .answer code, code { background:var(--code); border:1px solid var(--soft); padding:1px 4px; border-radius:2px; }
  .answer a.cite { font-size:.75em; vertical-align:super; line-height:0; }
  .answer strong { font-weight:700; }
  h2 { font:400 1.4rem/1.3 var(--serif); margin:24px 0 8px; border-bottom:1px solid var(--line); }
  ol.src { padding:0; margin:0; list-style:none; counter-reset:s; }
  ol.src li { counter-increment:s; padding:8px 10px 8px 34px; position:relative; border-bottom:1px solid var(--soft); }
  ol.src li:last-child { border-bottom:0; }
  ol.src li::before { content:counter(s); position:absolute; left:10px; top:8px; color:var(--muted); font-weight:700; }
  ol.src li.used { background:rgba(51,102,204,.08); } ol.src li.used > a { font-weight:700; }
  ol.src li:target { outline:2px solid var(--accent); outline-offset:-2px; }
  .meta { color:var(--muted); font-size:.8rem; font-weight:400; display:block; }
  .t span { display:inline; margin-right:14px; }
  .badge { display:inline-block; font-size:.68rem; text-transform:uppercase; padding:0 5px; border:1px solid var(--line); border-radius:2px; margin-right:6px; color:var(--muted); background:var(--bg); }
  details { margin-top:20px; border:1px solid var(--line); background:var(--card); } summary { cursor:pointer; padding:7px 10px; color:var(--link); }
  details > *:not(summary) { margin-left:10px; margin-right:10px; }
  pre { background:var(--code); border:1px solid var(--soft); padding:10px; overflow-x:auto; font-size:.8rem; line-height:1.45; }
  .err { color:var(--red); border:1px solid var(--red); background:rgba(221,51,51,.06); padding:10px 12px; margin-top:20px; }
  .steps { list-style:none; padding:10px 12px; margin:20px 0 0; font-size:.9rem; background:var(--card); border:1px solid var(--line); border-left:4px solid var(--accent); }
  .steps li { padding:2px 0; color:var(--muted); } .steps li.done { color:var(--fg); }
  .steps li::before { content:'\25CB'; display:inline-block; width:1.5em; }
  .steps li.done::before { content:'\2713'; color:var(--green); font-weight:700; }
  .steps li.run::before { content:''; width:.8em; height:.8em; margin-right:.7em; vertical-align:-1px; display:inline-block;
                          border:2px solid var(--line); border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .cursor::after { content:'\258D'; color:var(--accent); animation:blink 1s steps(2) infinite; }
  @keyframes blink { 50% { opacity:0; } }
  footer { margin-top:40px; padding-top:12px; border-top:1px solid var(--line); color:var(--muted); font-size:.8rem; }
  [hidden] { display:none !important; }
</style></head>
<body>
{{ nav|safe }}
<main>
<h1>Ask Wikipedia</h1>
<p class="sub">History of Finland · 29,460 articles in Finnish, Swedish and English · search by meaning in MariaDB, answer written from the articles it finds</p>

<form method="get" action="/ask" id="f">
  <input type="text" id="q" name="q" value="{{ q }}" placeholder="Ask in English, Finnish or Swedish" autofocus required>
  <select id="lang" name="lang" aria-label="Language of sources">
    <option value="">all languages</option>
    {% for code, name in [('fi','Finnish only'),('sv','Swedish only'),('en','English only')] %}
    <option value="{{ code }}" {{ 'selected' if lang == code }}>{{ name }}</option>{% endfor %}
  </select>
  <button type="submit" id="go">Ask</button>
</form>
<p class="ex">Try: {% for e in examples %}<a href="/ask?q={{ e|urlencode }}">{{ e }}</a>{% endfor %}</p>

<div class="grid">
<section>
<ul class="steps" id="steps" hidden>
  <li id="st-embed">Embedding the question (bge-m3)</li>
  <li id="st-search">Searching MariaDB vector index</li>
  <li id="st-gen">Writing the answer ({{ model }})</li>
</ul>
<div class="err" id="err" hidden></div>
<div class="answer" id="answer" hidden></div>
<p class="meta t" id="timings" hidden></p>
<details id="sql-wrap" hidden><summary>The SQL MariaDB ran</summary>
  <pre id="sql"></pre>
  <p class="meta">The question was embedded with bge-m3 (the model that made the stored vectors) and passed as the
  <code>?</code> vector. The vector search sits in a subquery so the vector index is used, then the articles are joined.
  Several language versions of one topic count once (same Wikidata id).</p>
</details>
</section>
<aside id="sources-wrap" hidden>
  <h2>Sources</h2>
  <ol class="src" id="sources"></ol>
</aside>
</div>

<footer>Text: Wikipedia, CC BY-SA 4.0. Vectors: bge-m3, 1024 dimensions, one per article.
Answers are written by an LLM from the retrieved articles only, but check the sources.</footer>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = t => t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
function inline(t) {
  t = esc(t);
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
  t = t.replace(/\[(\d+)\]/g, '<a class="cite" href="#s$1">[$1]</a>');
  return t;
}
// Small safe markdown: paragraphs, "- " / "* " lists, **bold**, *italic*, `code`, #-headings as bold lines.
function md(text) {
  const out = []; let list = false;
  const close = () => { if (list) { out.push('</ul>'); list = false; } };
  for (const raw of text.split(/\n/)) {
    const line = raw.trim();
    let m;
    if ((m = line.match(/^[-*•]\s+(.*)/))) { if (!list) { out.push('<ul>'); list = true; } out.push('<li>' + inline(m[1]) + '</li>'); }
    else if (!line) close();
    else { close(); const h = line.match(/^#{1,6}\s+(.*)/); out.push('<p>' + (h ? '<strong>' + inline(h[1]) + '</strong>' : inline(line)) + '</p>'); }
  }
  close();
  return out.join('');
}
const setStep = (id, state) => { const e = $(id); e.className = state; };
let source = null;

function ask(q, lang) {
  if (source) source.close();
  ['err','answer','timings','sources-wrap','sql-wrap'].forEach(i => $(i).hidden = true);
  $('answer').innerHTML = ''; $('sources').innerHTML = '';
  $('steps').hidden = false;
  ['st-embed','st-search','st-gen'].forEach(i => setStep(i, ''));
  setStep('st-embed', 'run');
  $('go').disabled = true;
  let text = '', t = {}, used = new Set();

  const finish = () => { $('go').disabled = false; $('answer').classList.remove('cursor'); if (source) source.close(); source = null; };
  const fail = msg => { $('err').textContent = msg; $('err').hidden = false; $('steps').hidden = true; finish(); };

  source = new EventSource('/stream?q=' + encodeURIComponent(q) + '&lang=' + encodeURIComponent(lang));
  source.addEventListener('embedded', e => {
    t = JSON.parse(e.data); setStep('st-embed','done'); $('st-embed').textContent = 'Question embedded (' + Math.round(t.embed_ms) + ' ms)'; setStep('st-search','run');
  });
  source.addEventListener('sources', e => {
    const d = JSON.parse(e.data);
    $('st-search').textContent = 'MariaDB returned ' + d.articles.length + ' articles (' + Math.round(d.search_ms) + ' ms)';
    setStep('st-search','done'); setStep('st-gen','run');
    $('sources').innerHTML = d.articles.map((a, i) =>
      '<li id="s' + (i+1) + '"><span class="badge">' + esc(a.lang) + '</span><a href="' + esc(a.url) + '" target="_blank" rel="noopener">' + esc(a.title) +
      '</a> <span class="meta">distance ' + a.distance.toFixed(3) + '<span class="cited"></span></span></li>').join('');
    $('sql').textContent = 'SET SESSION mhnsw_ef_search = ' + d.ef + ';\n\n' + d.sql;
    $('sources-wrap').hidden = false; $('sql-wrap').hidden = false;
  });
  source.addEventListener('token', e => {
    text += JSON.parse(e.data);
    $('answer').hidden = false; $('answer').classList.add('cursor');
    $('answer').innerHTML = md(text);
  });
  source.addEventListener('done', e => {
    const d = JSON.parse(e.data);
    $('st-gen').textContent = 'Answer written (' + (d.generate_ms/1000).toFixed(1) + ' s)'; setStep('st-gen','done');
    $('answer').innerHTML = md(text.trim());
    for (const m of text.matchAll(/\[(\d+)\]/g)) used.add(+m[1]);
    document.querySelectorAll('#sources li').forEach((li, i) => {
      if (used.has(i+1)) li.classList.add('used'); else li.querySelector('.cited').textContent = ' · retrieved, not cited';
    });
    $('timings').innerHTML = '<span>embed ' + Math.round(t.embed_ms) + ' ms</span><span>MariaDB vector search ' +
      Math.round(d.search_ms) + ' ms</span><span>answer ({{ model }}) ' + (d.generate_ms/1000).toFixed(1) + ' s</span>';
    $('timings').hidden = false;
    finish();
  });
  source.addEventListener('problem', e => fail(JSON.parse(e.data)));
  source.onerror = () => { if (source) fail('Connection to the server was lost.'); };
}

$('f').addEventListener('submit', e => {
  e.preventDefault();
  const q = $('q').value.trim(), lang = $('lang').value;
  if (!q) return;
  history.replaceState(null, '', '/ask?q=' + encodeURIComponent(q) + (lang ? '&lang=' + lang : ''));
  ask(q, lang);
});
if ($('q').value.trim()) ask($('q').value.trim(), $('lang').value);
</script>
</body></html>"""


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.route("/stream")
def stream():
    q = request.args.get("q", "").strip()
    lang = request.args.get("lang", "")
    if lang not in ("fi", "sv", "en"):
        lang = ""

    def events():
        try:
            if not q:
                raise ValueError("Empty question")
            vector, t_embed = rag.timed_embed(q)
            yield sse("embedded", dict(embed_ms=t_embed))
            articles, search_ms, sql = rag.search(vector, lang or None)
            yield sse("sources", dict(articles=[{k: a[k] for k in ("lang", "title", "url", "distance")} for a in articles],
                                      search_ms=search_ms, sql=sql, ef=rag.EF_SEARCH))
            t0 = time.perf_counter()
            for chunk in rag.generate_stream(q, articles):
                yield sse("token", chunk)
            yield sse("done", dict(search_ms=search_ms, generate_ms=(time.perf_counter() - t0) * 1000))
        except Exception as e:  # show the problem on the page instead of a stack trace
            yield sse("problem", f"{type(e).__name__}: {e}")

    return Response(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/ask")
def index():
    q = request.args.get("q", "").strip()
    lang = request.args.get("lang", "")
    if lang not in ("fi", "sv", "en"):
        lang = ""
    return render_template_string(PAGE, nav=hub.NAV, q=q, lang=lang, examples=EXAMPLES, model=rag.GEMINI_MODEL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    try:
        rag.embed("warm up")  # load bge-m3 into memory now so the first question is not slow
    except Exception as e:
        print("Embedding warm-up failed:", e)
    app.run(port=parser.parse_args().port, threaded=True)
