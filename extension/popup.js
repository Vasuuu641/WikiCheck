const API_BASE = "http://127.0.0.1:8020";   // the Aikakone server (api/): checks, Ask, and the drift page and stream

const content = document.getElementById("content");
const refreshBtn = document.getElementById("refreshBtn");
const titleEl = document.getElementById("pageTitle");

let mode = "conflicts";      // conflicts | gaps | ask | drift
let checkData = null;        // the last /api/consistency result: Conflicts and Missing here share it
let checkKey = "";           // which article checkData is for
let currentWiki = null;      // the article in the tab, or one picked from search results
let currentTabId = null;
let driftSource = null;
let loadSeq = 0;             // a slow Check that finishes after a mode switch must not paint over it

const LANG_NAMES = { fi: "Finnish", sv: "Swedish", en: "English", de: "German", fr: "French", es: "Spanish",
  ru: "Russian", it: "Italian", nl: "Dutch", pl: "Polish", pt: "Portuguese", ja: "Japanese", uk: "Ukrainian",
  ar: "Arabic", zh: "Chinese", et: "Estonian", no: "Norwegian", da: "Danish" };
const langName = l => LANG_NAMES[l] || l;

const CONFLICT_KINDS = {
  number: "Different number", date: "Different date", chronology: "Different order of events",
  causal_framing: "Different cause", naming: "Different name", scope_omission: "Different scope",
  neutrality: "Different framing", other: "Disagreement"
};

// ---------------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------------

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function parseWikiUrl(url) {
  if (!url) return null;
  try {
    const u = new URL(url);
    const m = u.hostname.match(/^([a-z]{2})\.wikipedia\.org$/);
    if (!m || !u.pathname.startsWith("/wiki/")) return null;
    const rawTitle = decodeURIComponent(u.pathname.slice(6));
    if (!rawTitle || rawTitle.includes(":")) return null;
    return { lang: m[1], title: rawTitle };
  } catch {
    return null;
  }
}

const isCheck = m => m === "conflicts" || m === "gaps";
const wikiKey = w => (w ? `${w.lang}:${w.title}` : "");

function setCounts(data) {
  const show = data && data.found && data.article?.wikidata_id;
  document.getElementById("nConflicts").textContent = show ? (data.conflicts || []).length : "";
  document.getElementById("nGaps").textContent = show ? (data.gaps || []).length : "";
}

const pageTitle = () => (currentWiki ? currentWiki.title.replace(/_/g, " ") : "");

function setHeader() {
  if (currentWiki) {
    titleEl.innerHTML = `${esc(pageTitle())}<span class="edition">${esc(langName(currentWiki.lang))} Wikipedia</span>`;
    titleEl.title = pageTitle();
  } else {
    titleEl.textContent = "Wikipedia, checked across languages and time";
    titleEl.title = "";
  }
}

// Quotes come from the stored article text, which keeps Markdown tables and reference marks.
// A table row "| a | b | c |" becomes cells; [12], *, # and ` are dropped.
function cleanParts(text) {
  return String(text || "")
    .split("|")
    .map(p => p.replace(/\[\d+\]/g, "").replace(/[#*`]/g, "").replace(/\s+/g, " ").trim())
    .filter(p => p && !/^[-:\s]+$/.test(p));
}

function quoteHtml(text) {
  const parts = cleanParts(text);
  if (parts.length <= 1) return esc(parts[0] || "");
  return parts.map(p => `<span class="cell">${esc(p)}</span>`).join("");
}

// Link that opens the article scrolled to the quote (text fragment), using its longest clean part.
function fragmentUrl(baseUrl, text) {
  const parts = cleanParts(text).filter(p => p.length >= 6);
  if (!parts.length) return baseUrl;
  const best = parts.sort((a, b) => b.length - a.length)[0];
  return `${baseUrl}#:~:text=${encodeURIComponent(best.split(" ").slice(0, 7).join(" "))}`;
}

// The model writes "Article A ... Article B": name the sides the way the reader sees them.
function nameSides(text, aLabel, bLabel) {
  const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
  return String(text || "").replace(/\b[Aa]rticle ([AB])\b/g, (_, side, offset, whole) => {
    const label = side === "A" ? aLabel : bLabel;
    const before = whole.slice(0, offset).trimEnd();
    return (!before || /[.!?:]$/.test(before)) ? cap(label) : label;   // capital only at a sentence start
  });
}

async function highlightOnCurrentPage(text, btn) {
  if (!currentTabId) return;
  const target = cleanParts(text).sort((a, b) => b.length - a.length)[0] || text;
  try {
    const res = await chrome.tabs.sendMessage(currentTabId, { action: "highlight", text: target });
    btn.textContent = res?.ok ? "Shown on the page" : "Not found on the page";
  } catch {
    btn.textContent = "Reload the page, then try again";
  }
}

const spinnerState = (lead, note = "") =>
  `<div class="state"><div class="spinner"></div>${lead}${note ? `<small>${note}</small>` : ""}</div>`;

// ---------------------------------------------------------------------------------------------
// Check: conflicts and missing content across language editions (api/consistency.py)
// ---------------------------------------------------------------------------------------------

function renderNotWiki() {
  refreshBtn.hidden = true;
  setHeader();
  content.innerHTML = `
    <div class="state">
      Open a Wikipedia article to compare it with its other language editions.
      <div class="demo-links">
        <a href="https://sv.wikipedia.org/wiki/Regeringen_Aura_II" target="_blank">Regeringen Aura II (Swedish): six conflicting dates</a>
        <a href="https://en.wikipedia.org/wiki/Hauho" target="_blank">Hauho (English): what the Finnish edition adds</a>
        <a href="https://fi.wikipedia.org/wiki/Talvisota" target="_blank">Talvisota (Finnish): open it, then choose Drift to see 23 years of edits</a>
      </div>
    </div>`;
}

function conflictHtml(c, idx) {
  const kind = CONFLICT_KINDS[c.type] || CONFLICT_KINDS.other;
  const here = "this page";
  const there = `the ${langName(c.other.lang)} edition`;
  const expl = nameSides(c.explanation, c.current_is_a === false ? there : here, c.current_is_a === false ? here : there);
  return `
    <div class="item">
      <div class="item-head">
        <span class="kind">${esc(kind)}</span>
        <span class="aside" title="How sure the two checks were">${Math.round((c.confidence || 0) * 100)}% sure</span>
      </div>
      <p class="expl">${esc(expl)}</p>
      <div class="side this">
        <div class="side-label">This page</div>
        <div>
          <div class="quote">${quoteHtml(c.current.quote)}</div>
          <button class="find" data-idx="${idx}">Show on the page</button>
        </div>
      </div>
      <div class="side other">
        <div class="side-label"><a href="${esc(fragmentUrl(c.other.url, c.other.quote))}" target="_blank" title="${esc(c.other.title)}">${esc(langName(c.other.lang))}</a></div>
        <div class="quote">${quoteHtml(c.other.quote)}</div>
      </div>
    </div>`;
}

function gapHtml(g) {
  return `
    <div class="item">
      <div class="item-head">
        <span class="kind gap">${g.importance === "high" ? "Important" : "Useful"}, only in ${esc(langName(g.source.lang))}</span>
        <a class="aside" href="${esc(fragmentUrl(g.source.url, g.paragraph))}" target="_blank">Read it there</a>
      </div>
      <p class="expl">${esc(g.summary)}</p>
      <details>
        <summary>The ${esc(langName(g.source.lang))} paragraph</summary>
        <div class="para">${esc(cleanParts(g.paragraph).join(" · "))}</div>
      </details>
    </div>`;
}

function renderData(data) {
  if (!data.found) {
    content.innerHTML = `<div class="state">This article is not in the database, and it could not be fetched from Wikipedia.
      <small>${esc(currentWiki.lang)}: ${esc(pageTitle())}</small></div>`;
    return;
  }
  if (!data.article.wikidata_id) {
    content.innerHTML = `<div class="state">This article is not linked to other language editions on Wikidata, so there is nothing to compare it with.</div>`;
    return;
  }

  const confs = data.conflicts || [];
  const gaps = data.gaps || [];

  const editions = (data.siblings || [])
    .map(s => `<a href="${esc(s.url)}" target="_blank" title="${esc(s.title)}">${esc(langName(s.lang))}</a>`).join(", ");
  const also = `
    <p class="also">Compared with ${editions || "no other edition"}.
      <span title="${data.cached ? "Read from MariaDB" : "Analysed now and stored in MariaDB"}">${data.cached ? "Stored result" : "Analysed now"}, ${data.took_ms} ms.</span></p>`;

  const list = mode === "conflicts"
    ? (confs.length ? confs.map(conflictHtml).join("")
        : `<div class="state">No statement here contradicts the other editions.</div>`)
    : (gaps.length ? gaps.map(gapHtml).join("")
        : `<div class="state">The other editions add nothing important that this page lacks.</div>`);

  content.innerHTML = also + list;
  content.querySelectorAll(".find").forEach(btn => btn.addEventListener("click", () => {
    const c = confs[Number(btn.dataset.idx)];
    if (c) highlightOnCurrentPage(c.current.quote, btn);
  }));
}

async function load(refresh = false) {
  const seq = ++loadSeq;
  const key = wikiKey(currentWiki);
  setHeader();
  refreshBtn.hidden = false;
  if (!refresh && checkData && checkKey === key) {   // switching Conflicts <-> Missing here: no new request
    renderData(checkData);
    return;
  }
  const firstLook = checkKey !== key;
  checkData = null;
  checkKey = key;
  setCounts(null);
  content.innerHTML = spinnerState("Comparing this article with its other language editions…",
    "A stored result comes back at once. A first analysis takes about 20 s.");
  try {
    const qs = new URLSearchParams({ lang: currentWiki.lang, title: currentWiki.title, compute: "true",
                                     refresh: refresh ? "true" : "false" });
    const res = await fetch(`${API_BASE}/api/consistency?${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (seq !== loadSeq || !isCheck(mode)) return;
    checkData = data;
    setCounts(data);
    // First look at an article with no conflict but some missing content: open Missing here.
    if (firstLook && mode === "conflicts" && !(data.conflicts || []).length && (data.gaps || []).length) setMode("gaps", false);
    renderData(data);
  } catch (err) {
    if (seq !== loadSeq || !isCheck(mode)) return;
    checkKey = "";
    content.innerHTML = `<div class="state">The Aikakone service at ${esc(API_BASE)} did not answer.
      <small>Start it with <code>make run</code> in the repository, then reopen this popup. (${esc(err.message)})</small></div>`;
  }
}

refreshBtn.addEventListener("click", () => currentWiki && load(true));

// ---------------------------------------------------------------------------------------------
// Modes
// ---------------------------------------------------------------------------------------------

function stopDrift() {
  if (driftSource) { driftSource.close(); driftSource = null; }
}

function setMode(next, render = true) {
  mode = next;
  loadSeq++;          // drop whatever the previous mode was still waiting for
  stopDrift();
  document.querySelectorAll("#modes .mode").forEach(b => {
    const on = b.dataset.mode === next;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  });
  refreshBtn.hidden = !isCheck(next) || !currentWiki;
  if (!render) return;
  if (next === "ask") renderAsk();
  else if (next === "drift") renderDrift();
  else if (currentWiki) load(false);
  else renderNotWiki();
}

document.querySelectorAll("#modes .mode").forEach(b => b.addEventListener("click", () => {
  if (b.dataset.mode !== mode) setMode(b.dataset.mode);
}));

// ---------------------------------------------------------------------------------------------
// Ask: cited answer from MariaDB passages, today or as of a date (api/ask.py)
// ---------------------------------------------------------------------------------------------

function withCitations(text) {
  return esc(text)
    .replace(/\[(\d+)\]/g, '<a class="cite" href="#cit$1">[$1]</a>')
    .replace(/\n+/g, "<br>");
}

function renderAsk() {
  const title = pageTitle();
  content.innerHTML = `
    <form id="askForm" class="ask-form">
      <textarea id="askQ" rows="2" aria-label="Question" placeholder="Ask a question to find cited sources (English, Finnish, or Swedish)">${
        title ? esc(`What do Wikipedia sources say about ${title}?`) : ""}</textarea>
      <div class="ask-row">
        <label title="Check sources as they stood on this date. Works for Finnish articles whose history is loaded.">As Wikipedia stood on
          <input id="askAsOf" type="date" min="2003-01-01"></label>
        <button type="submit" class="primary">Find sources</button>
      </div>
    </form>
    <div id="askOut"><div class="state">Ask about any topic or claim to get cited passages, neutrality flags, and sources that disagree.</div></div>`;
  const form = document.getElementById("askForm");
  form.addEventListener("submit", e => {
    e.preventDefault();
    const q = document.getElementById("askQ").value.trim();
    if (q) runAsk(q, document.getElementById("askAsOf").value);
  });
  document.getElementById("askQ").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
}

async function runAsk(question, asOf) {
  const seq = ++loadSeq;
  const out = document.getElementById("askOut");
  out.innerHTML = spinnerState("Finding passages and writing the answer…",
    "About 10 to 20 s, including a neutrality review of every source.");
  try {
    const body = { question, mode: "editor" };
    if (asOf) body.as_of = asOf;
    const res = await fetch(`${API_BASE}/api/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const d = await res.json();
    if (!res.ok || d.error) throw new Error(d.error || `HTTP ${res.status}`);
    if (seq !== loadSeq || mode !== "ask") return;

    const cits = (d.citations || []).map(c => {
      const flagged = c.neutrality && c.neutrality.level === "flagged";
      return `
        <div class="item" id="cit${c.n}">
          <div class="item-head">
            <span><span class="source-n">[${c.n}]</span><a href="${esc(c.url)}" target="_blank">${esc(c.title)}</a></span>
            ${c.edit_url ? `<a class="aside" href="${esc(c.edit_url)}" target="_blank">Edit</a>` : ""}
          </div>
          ${flagged ? `<p class="expl"><span class="kind">${esc(c.neutrality.category || "Wording")}:</span> ${esc(c.neutrality.reason || "")}</p>` : ""}
          <details><summary>The passage</summary><div class="para">${esc(c.passage)}</div></details>
        </div>`;
    }).join("");

    const confs = (d.conflicts || []).map(c => `
      <div class="item">
        <p class="expl">${esc(c.about || "")}</p>
        <div class="side this"><div class="side-label">Source [${esc(c.a?.n)}]</div><div class="quote">${esc(c.a?.phrase)}</div></div>
        <div class="side other"><div class="side-label">Source [${esc(c.b?.n)}]</div><div class="quote">${esc(c.b?.phrase)}</div></div>
      </div>`).join("");

    const flaggedN = (d.citations || []).filter(c => c.neutrality?.level === "flagged").length;
    out.innerHTML = `
      <div class="answer">${withCitations(d.answer || "")}</div>
      <p class="also">${asOf ? `As Wikipedia stood on ${esc(asOf)}. ` : ""}${
        flaggedN ? `${flaggedN} source${flaggedN === 1 ? "" : "s"} flagged for wording. ` : "Every source reads as neutral. "}${(d.took_ms / 1000).toFixed(1)} s.</p>
      ${confs ? `<h2 class="section-label">Sources that disagree</h2>${confs}` : ""}
      ${cits ? `<h2 class="section-label">Sources</h2>${cits}` : ""}`;
  } catch (err) {
    if (seq !== loadSeq || !document.getElementById("askOut")) return;
    out.innerHTML = `<div class="state">No answer: ${esc(err.message)}</div>`;
  }
}

// ---------------------------------------------------------------------------------------------
// Drift: how the article's position on its main claims changed, edit by edit (pacome/ site)
// ---------------------------------------------------------------------------------------------

function renderDrift() {
  const title = pageTitle();
  content.innerHTML = `
    <form id="driftForm" class="field-row">
      <input id="driftQ" type="text" value="${esc(title)}" aria-label="Article or question"
             placeholder="An article, or a question such as Who started the Winter War?">
      <button type="submit">Trace versions</button>
    </form>
    <div id="driftOut"><div class="state">
      Track how an article's claims changed across versions, down to the exact edit.
      <div class="demo-links"><a href="${API_BASE}/drift?fixture=1" target="_blank">Watch a recorded run on the full page</a></div>
    </div></div>`;
  document.getElementById("driftForm").addEventListener("submit", e => {
    e.preventDefault();
    const q = document.getElementById("driftQ").value.trim();
    if (q) runDrift(q);
  });
  if (title) runDrift(title);
}

// One colour per stance, the same as the chart on the full page, whichever side of "became" it is on.
const STANCE_CLASS = { "stated as fact": "st-fact", "stated with attribution": "st-attr", "not addressed": "st-none",
  "presented as disputed": "st-disp", "denied": "st-deny" };
const stanceChip = w => `<span class="stance ${STANCE_CLASS[String(w).trim().toLowerCase()] || "st-none"}">${esc(w)}</span>`;

// "Claim label: stated as fact until February 2018, not addressed since"
function splitHeadline(h) {
  const m = String(h || "").match(/^(.*?):\s*(.+?) until (\w+ \d{4}),\s*(.+?) since$/);
  return m ? { claim: m[1], was: m[2], when: m[3], now: m[4] } : null;
}

function runDrift(q) {
  stopDrift();
  const out = document.getElementById("driftOut");
  // On an article, q is its title and lang its edition: the service then runs on that article alone.
  const onPage = currentWiki && q === pageTitle() ? `&lang=${encodeURIComponent(currentWiki.lang)}` : "";
  const fullUrl = `${API_BASE}/drift?q=${encodeURIComponent(q)}${onPage}`;
  const SHOWN = 12;   // the popup is small: newest first, the rest are on the full page
  let articles = [], claims = {}, changes = [], summary = "", finished = false;
  let status = "Finding the article and its edit history…";

  const draw = () => {
    if (mode !== "drift" || !document.getElementById("driftOut")) return;
    const arts = articles.map(a =>
      `<a href="${esc(a.url)}" target="_blank">${esc(a.title)}</a> (${Number(a.n_revs).toLocaleString("en")} versions, since ${esc((a.first || "").slice(0, 4))})`).join("; ");
    const items = changes.slice().sort((x, y) => (x.t < y.t ? 1 : -1)).slice(0, SHOWN).map(c => {
      const h = splitHeadline(c.headline);
      const label = h ? h.claim : ((claims[c.claim] || {}).label || c.claim);
      const diff = `https://fi.wikipedia.org/w/index.php?diff=${encodeURIComponent(c.rev_after)}&oldid=${encodeURIComponent(c.rev_before)}`;
      return `
        <li>
          <span class="when">${esc(h ? h.when : (c.t || "").slice(0, 7))}</span>
          <span class="claim move">${esc(label)}</span>
          ${h ? `<span class="move">${stanceChip(h.was)} became ${stanceChip(h.now)}</span>` : ""}
          <span class="edit-note">${c.comment ? `Edit summary: “${esc(c.comment)}”. ` : ""}<a href="${esc(diff)}" target="_blank">See the edit</a></span>
        </li>`;
    }).join("");

    out.innerHTML = `
      ${arts ? `<p class="also">${arts}</p>` : ""}
      ${finished
        ? `<p class="run-status"><strong>${changes.length} change${changes.length === 1 ? "" : "s"} of position</strong>, newest first. <a href="${esc(fullUrl)}" target="_blank">Open the full timeline</a></p>`
        : `<p class="run-status"><span class="spinner inline"></span>${esc(status)}</p>`}
      ${summary ? `<p class="summary">${esc(summary)}</p>` : ""}
      ${items ? `<ol class="timeline">${items}</ol>` : (finished ? `<div class="state">The article kept the same position on every claim.</div>` : "")}
      ${changes.length > SHOWN ? `<p class="also"><a href="${esc(fullUrl)}" target="_blank">${changes.length - SHOWN} earlier changes on the full timeline</a></p>` : ""}`;
  };
  const on = (name, fn) => driftSource.addEventListener(name, e => { fn(JSON.parse(e.data)); draw(); });
  const fail = msg => {
    stopDrift();
    if (!document.getElementById("driftOut")) return;
    out.innerHTML = `<div class="state">${esc(msg)}
      <div class="demo-links"><a href="${esc(fullUrl)}" target="_blank">Open the drift page</a></div></div>`;
  };

  draw();
  driftSource = new EventSource(`${API_BASE}/api/drift/stream?q=${encodeURIComponent(q)}${onPage}`);
  on("sources", d => { articles = d.articles || []; status = "Writing the claims to follow…"; });
  on("claims", d => { (d.claims || []).forEach(c => { claims[c.id] = c; }); status = `Reading old versions against ${Object.keys(claims).length} claims…`; });
  on("probes", d => { status = `Narrowing down the edits (round ${(d.round || 0) + 1})…`; });
  on("change", d => { changes.push(d); });
  // The summary is "ANSWER: ..." followed by more labelled sections: the popup keeps the answer.
  // Its [n] marks point at the full page's source list, which the popup does not show: they are dropped.
  on("summary", d => {
    summary = (d.text || "").replace(/^ANSWER:\s*/, "").split(/\n\s*[A-Z][A-Z ]+:/)[0]
      .replace(/\s*\[\d+\]/g, "").trim();
  });
  on("done", () => { finished = true; stopDrift(); });
  driftSource.addEventListener("problem", e => {
    let msg = e.data;
    try { msg = JSON.parse(e.data); } catch {}
    fail(String(typeof msg === "string" ? msg : (msg.message || JSON.stringify(msg))).replace(/^\w+Error:\s*/, ""));
  });
  driftSource.onerror = () => {
    if (driftSource && !finished) fail(`The Aikakone server at ${API_BASE} did not answer. Start it with make run in the repository.`);
  };
}

// ---------------------------------------------------------------------------------------------
// start
// ---------------------------------------------------------------------------------------------

chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
  currentTabId = tabs[0]?.id ?? null;
  currentWiki = parseWikiUrl(tabs[0]?.url);
  setMode("conflicts");
});
