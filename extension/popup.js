const API_BASE = "http://127.0.0.1:8020";   // FastAPI: consistency, ask, trace (api/)
const SITE_BASE = "http://127.0.0.1:8001";  // Flask site: opinion drift and the full-page tools (pacome/)

const content = document.getElementById("content");
const refreshBtn = document.getElementById("refreshBtn");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const titleEl = document.getElementById("pageTitle");

let mode = "check";          // check | ask | drift
let checkTab = "conflicts";  // conflicts | gaps, inside Check
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
  if (checkTab === "conflicts" && confs.length === 0 && gaps.length > 0) checkTab = "gaps";

  const editions = (data.siblings || [])
    .map(s => `<a href="${esc(s.url)}" target="_blank" title="${esc(s.title)}">${esc(langName(s.lang))}</a>`).join(", ");
  const also = `
    <p class="also">Compared with ${editions || "no other edition"}.
      <span title="${data.cached ? "Read from MariaDB" : "Analysed now and stored in MariaDB"}">${data.cached ? "Stored result" : "Analysed now"}, ${data.took_ms} ms.</span></p>`;

  const tabs = `
    <div class="tabs" role="tablist">
      <button class="tab ${checkTab === "conflicts" ? "active" : ""}" data-tab="conflicts" role="tab">Conflicts<span class="n">${confs.length}</span></button>
      <button class="tab ${checkTab === "gaps" ? "active" : ""}" data-tab="gaps" role="tab">Missing here<span class="n">${gaps.length}</span></button>
    </div>`;

  const list = checkTab === "conflicts"
    ? (confs.length ? confs.map(conflictHtml).join("")
        : `<div class="state">No statement here contradicts the other editions.</div>`)
    : (gaps.length ? gaps.map(gapHtml).join("")
        : `<div class="state">The other editions add nothing important that this page lacks.</div>`);

  const sql = data.sql ? `
    <details><summary>The SQL MariaDB ran</summary><pre>${esc(data.sql.conflicts)}\n\n${esc(data.sql.gaps)}</pre></details>` : "";

  content.innerHTML = also + tabs + list + sql;
  content.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => {
    checkTab = btn.dataset.tab;
    renderData(data);
  }));
  content.querySelectorAll(".find").forEach(btn => btn.addEventListener("click", () => {
    const c = confs[Number(btn.dataset.idx)];
    if (c) highlightOnCurrentPage(c.current.quote, btn);
  }));
}

async function load(refresh = false) {
  const seq = ++loadSeq;
  setHeader();
  refreshBtn.hidden = false;
  content.innerHTML = spinnerState("Comparing this article with its other language editions…",
    "A stored result comes back at once. A first analysis takes about 20 s.");
  try {
    const qs = new URLSearchParams({ lang: currentWiki.lang, title: currentWiki.title, compute: "true",
                                     refresh: refresh ? "true" : "false" });
    const res = await fetch(`${API_BASE}/api/consistency?${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (seq !== loadSeq || mode !== "check") return;
    renderData(data);
  } catch (err) {
    if (seq !== loadSeq || mode !== "check") return;
    content.innerHTML = `<div class="state">The Aikakone service at ${esc(API_BASE)} did not answer.
      <small>Start it with <code>make api</code> in the repository, then reopen this popup. (${esc(err.message)})</small></div>`;
  }
}

refreshBtn.addEventListener("click", () => currentWiki && load(true));

async function runSearch(q) {
  const seq = ++loadSeq;
  refreshBtn.hidden = true;
  content.innerHTML = spinnerState(`Searching for <strong>${esc(q)}</strong>…`);
  try {
    const res = await fetch(`${API_BASE}/api/consistency/search?` + new URLSearchParams({ q }));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (seq !== loadSeq || mode !== "check") return;

    const conf = (d.conflicts || []).map(c => `
      <div class="item">
        <div class="item-head">
          <span class="kind">${esc(CONFLICT_KINDS[c.conflict_type] || CONFLICT_KINDS.other)}</span>
          <span class="aside">${esc(c.title_a)}</span>
        </div>
        <p class="expl">${esc(nameSides(c.explanation, `the ${langName(c.lang_a)} edition`, `the ${langName(c.lang_b)} edition`))}</p>
        <div class="links">
          <a href="${esc(fragmentUrl(c.url_a, c.quote_a))}" target="_blank">Read it in ${esc(langName(c.lang_a))}</a>
          <a href="${esc(fragmentUrl(c.url_b, c.quote_b))}" target="_blank">Read it in ${esc(langName(c.lang_b))}</a>
        </div>
      </div>`).join("");

    const gap = (d.gaps || []).map(g => `
      <div class="item">
        <div class="item-head">
          <span class="kind gap">Missing in ${esc(langName(g.dst_lang))}</span>
          <span class="aside">${esc(g.dst_title)}</span>
        </div>
        <p class="expl">${esc(g.summary)}</p>
        <div class="links"><a href="${esc(fragmentUrl(g.src_url, g.paragraph))}" target="_blank">Read it in ${esc(langName(g.src_lang))}</a></div>
      </div>`).join("");

    const arts = (d.articles || []).map(a => `
      <div class="hit">
        <span><a href="${esc(a.url)}" target="_blank">${esc(a.title)}</a> <span class="aside">${esc(langName(a.lang))}</span></span>
        <button class="quiet-btn" data-lang="${esc(a.lang)}" data-title="${esc(a.title)}">${
          a.analyzed ? `${a.conflicts_count} conflict${a.conflicts_count === 1 ? "" : "s"}, ${a.gaps_count} missing` : "Check it"}</button>
      </div>`).join("");

    content.innerHTML = `
      <p class="also">Results for “${esc(d.query)}”, ${d.took_ms} ms.
        ${currentWiki ? `<a href="#" id="backToPage" class="back">Back to the article</a>` : ""}</p>
      ${conf ? `<h2 class="section-label">Conflicts already found</h2>${conf}` : ""}
      ${gap ? `<h2 class="section-label">Missing content already found</h2>${gap}` : ""}
      ${arts ? `<h2 class="section-label">Closest articles by meaning</h2>${arts}` : ""}
      ${(!conf && !gap && !arts) ? `<div class="state">Nothing matches “${esc(d.query)}”. Try the name of a person, place or event.</div>` : ""}`;

    content.querySelectorAll(".hit .quiet-btn").forEach(btn => btn.addEventListener("click", () => {
      currentWiki = { lang: btn.dataset.lang, title: btn.dataset.title };
      load(false);
    }));
    const back = document.getElementById("backToPage");
    if (back) back.addEventListener("click", e => {
      e.preventDefault();
      chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
        currentWiki = parseWikiUrl(tabs[0]?.url);
        currentWiki ? load(false) : renderNotWiki();
      });
    });
  } catch (err) {
    if (seq !== loadSeq) return;
    content.innerHTML = `<div class="state">The search did not run: the service at ${esc(API_BASE)} did not answer.<small>${esc(err.message)}</small></div>`;
  }
}

searchForm.addEventListener("submit", e => {
  e.preventDefault();
  const q = searchInput.value.trim();
  if (!q) return;
  setMode("check", false);
  runSearch(q);
});

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
  refreshBtn.hidden = next !== "check" || !currentWiki;
  searchForm.hidden = next !== "check";
  if (!render) return;
  if (next === "ask") renderAsk();
  else if (next === "drift") renderDrift();
  else if (currentWiki) load(false);
  else renderNotWiki();
}

document.querySelectorAll("#modes .mode").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

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
      <textarea id="askQ" rows="2" aria-label="Question" placeholder="Ask in English, Finnish or Swedish">${
        title ? esc(`What does Wikipedia say about ${title}?`) : ""}</textarea>
      <div class="ask-row">
        <label title="Answer from the articles as they stood on this date. Works for Finnish articles whose history is loaded.">As Wikipedia stood on
          <input id="askAsOf" type="date" min="2003-01-01"></label>
        <button type="submit" class="primary">Ask</button>
      </div>
    </form>
    <div id="askOut"><div class="state">The answer is written only from passages MariaDB finds, and cites each one.</div></div>`;
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
      ${cits ? `<h2 class="section-label">Sources</h2>${cits}` : ""}
      ${d.sql ? `<details><summary>The SQL MariaDB ran</summary><pre>${esc(d.sql)}</pre></details>` : ""}`;
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
      <button type="submit">Trace</button>
    </form>
    <div id="driftOut"><div class="state">
      Follow how the Finnish articles changed their position on the same claims, down to the edit that changed it.
      <div class="demo-links"><a href="${SITE_BASE}/drift?fixture=1" target="_blank">Watch a recorded run on the full page</a></div>
    </div></div>`;
  document.getElementById("driftForm").addEventListener("submit", e => {
    e.preventDefault();
    const q = document.getElementById("driftQ").value.trim();
    if (q) runDrift(q);
  });
  if (title) runDrift(title);
}

// "Claim label: stated as fact until February 2018, not addressed since"
function splitHeadline(h) {
  const m = String(h || "").match(/^(.*?):\s*(.+?) until (\w+ \d{4}),\s*(.+?) since$/);
  return m ? { claim: m[1], was: m[2], when: m[3], now: m[4] } : null;
}

function runDrift(q) {
  stopDrift();
  const out = document.getElementById("driftOut");
  const fullUrl = `${SITE_BASE}/drift?q=${encodeURIComponent(q)}`;
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
          ${h ? `<span class="move"><span class="was">${esc(h.was)}</span> became <span class="now">${esc(h.now)}</span></span>` : ""}
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
  // On an article, q is its title and lang its edition: the service then runs on that article alone.
  const onPage = currentWiki && q === pageTitle() ? `&lang=${encodeURIComponent(currentWiki.lang)}` : "";
  driftSource = new EventSource(`${SITE_BASE}/api/drift/stream?q=${encodeURIComponent(q)}${onPage}`);
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
    if (driftSource && !finished) fail(`The drift service at ${SITE_BASE} did not answer. Start the site with pacome/scripts/run.sh --port 8001.`);
  };
}

// ---------------------------------------------------------------------------------------------
// start
// ---------------------------------------------------------------------------------------------

chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
  currentTabId = tabs[0]?.id ?? null;
  currentWiki = parseWikiUrl(tabs[0]?.url);
  setMode("check");
});
