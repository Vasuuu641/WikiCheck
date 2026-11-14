const API_BASE = "http://127.0.0.1:8020";   // FastAPI: consistency, ask, trace (api/)
const SITE_BASE = "http://127.0.0.1:8001";  // Flask site: opinion drift and the full-page tools (pacome/)
const content = document.getElementById("content");
const refreshBtn = document.getElementById("refreshBtn");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");

let activeTab = "conflicts";
let currentWiki = null;
let currentTabId = null;

function fragmentUrl(baseUrl, text) {
  const parts = String(text || "")
    .split("|")
    .map(p => p.replace(/\[\d+\]/g, "").replace(/[#*`]/g, "").replace(/\s+/g, " ").trim())
    .filter(p => p.length >= 6);
  if (!parts.length) return baseUrl;
  const best = parts.sort((a, b) => b.length - a.length)[0];
  const words = best.split(" ").slice(0, 7).join(" ");
  return `${baseUrl}#:~:text=${encodeURIComponent(words)}`;
}

async function highlightOnCurrentPage(text, btn) {
  if (!currentTabId) return;
  try {
    const res = await chrome.tabs.sendMessage(currentTabId, { action: "highlight", text });
    if (btn) btn.textContent = res?.ok ? "Highlighted ✓" : "Not found on page";
  } catch {
    if (btn) btn.textContent = "Reload page to highlight";
  }
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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

function renderNotWiki() {
  refreshBtn.hidden = true;
  content.innerHTML = `
    <div class="state">
      Open an article on <strong>Wikipedia</strong> (any 2-letter language edition) to check cross-language conflicts and missing content.
      <div class="demo-links">
        <a href="https://sv.wikipedia.org/wiki/Regeringen_Aura_II" target="_blank">sv: Regeringen Aura II (conflicts demo)</a>
        <a href="https://en.wikipedia.org/wiki/Hauho" target="_blank">en: Hauho (missing content demo)</a>
        <a href="https://en.wikipedia.org/wiki/Metso" target="_blank">en: Metso (missing content demo)</a>
      </div>
    </div>`;
}

function renderData(data) {
  if (!data.found) {
    content.innerHTML = `<div class="state">Article not found in the local Finnish-history dataset (${esc(currentWiki.lang)}:${esc(currentWiki.title)}).</div>`;
    return;
  }

  const confs = data.conflicts || [];
  const gaps = data.gaps || [];
  if (activeTab === "conflicts" && confs.length === 0 && gaps.length > 0) activeTab = "gaps";

  const siblingsHtml = (data.siblings || [])
    .map(s => `<a class="pill" href="${esc(s.url)}" target="_blank">${esc(s.lang)}: ${esc(s.title)}</a>`)
    .join("");

  const metaHtml = `
    <div class="meta">
      <div class="meta-title">${esc(data.article.lang)}: ${esc(data.article.title)}</div>
      <div class="meta-row">
        <span>${data.cached ? "Cached" : "Freshly analyzed"} (${data.took_ms} ms)</span>
        ${data.article.wikidata_id ? `<span class="pill">${esc(data.article.wikidata_id)}</span>` : ""}
        ${siblingsHtml}
      </div>
    </div>`;

  const tabsHtml = `
    <div class="tabs">
      <button class="tab ${activeTab === "conflicts" ? "active" : ""}" data-tab="conflicts">
        Conflicts (${confs.length})
      </button>
      <button class="tab ${activeTab === "gaps" ? "active" : ""}" data-tab="gaps">
        Missing Content (${gaps.length})
      </button>
    </div>`;

  let listHtml = "";
  if (activeTab === "conflicts") {
    listHtml = confs.length
      ? confs.map((c, idx) => `
        <div class="card">
          <div class="card-head">
            <span class="badge conflict">${esc(c.type)}</span>
            <span class="conf">conf ${Math.round((c.confidence || 0) * 100)}%</span>
          </div>
          <p class="expl">${esc(c.explanation)}</p>
          <div class="quotes">
            <div class="quote-box">
              <div class="quote-label">
                <span>This page (${esc(c.current.lang)})</span>
                <button class="hl-btn" data-idx="${idx}">Highlight ⌖</button>
              </div>
              <div class="quote-text">“${esc(c.current.quote)}”</div>
            </div>
            <div class="quote-box">
              <div class="quote-label">
                <a href="${esc(fragmentUrl(c.other.url, c.other.quote))}" target="_blank">${esc(c.other.lang)}: ${esc(c.other.title)} ↗</a>
              </div>
              <div class="quote-text">“${esc(c.other.quote)}”</div>
            </div>
          </div>
        </div>`).join("")
      : `<div class="state">No confirmed cross-language conflicts found for this article.</div>`;
  } else {
    listHtml = gaps.length
      ? gaps.map(g => `
        <div class="card">
          <div class="card-head">
            <span class="badge ${esc(g.importance)}">${esc(g.importance)}</span>
            <a class="pill" href="${esc(fragmentUrl(g.source.url, g.paragraph))}" target="_blank">Highlight in ${esc(g.source.lang)}: ${esc(g.source.title)} ↗</a>
          </div>
          <p class="expl">${esc(g.summary)}</p>
          <details>
            <summary>Show source paragraph (${esc(g.source.lang)})</summary>
            <div class="para">${esc(g.paragraph)}</div>
          </details>
        </div>`).join("")
      : `<div class="state">No high-value missing sections detected compared to other languages.</div>`;
  }

  const sqlHtml = data.sql ? `
    <details>
      <summary>Show MariaDB SQL</summary>
      <pre>${esc(data.sql.conflicts)}\n\n${esc(data.sql.gaps)}</pre>
    </details>` : "";

  content.innerHTML = metaHtml + tabsHtml + listHtml + sqlHtml;
  content.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      activeTab = btn.dataset.tab;
      renderData(data);
    });
  });
  content.querySelectorAll(".hl-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const c = confs[Number(btn.dataset.idx)];
      if (c) highlightOnCurrentPage(c.current.quote, btn);
    });
  });
}

async function load(refresh = false) {
  refreshBtn.hidden = false;
  content.innerHTML = `
    <div class="state">
      <div class="spinner"></div>
      Checking <strong>${esc(currentWiki.lang)}:${esc(currentWiki.title)}</strong>…<br>
      <small>Instant if cached; ~20s if analyzing for the first time.</small>
    </div>`;
  try {
    const qs = new URLSearchParams({
      lang: currentWiki.lang,
      title: currentWiki.title,
      compute: "true",
      refresh: refresh ? "true" : "false"
    });
    const res = await fetch(`${API_BASE}/api/consistency?${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderData(data);
  } catch (err) {
    content.innerHTML = `
      <div class="state">
        Could not reach Aikakone API at <code>${esc(API_BASE)}</code>.<br>
        <small>${esc(err.message)}</small>
      </div>`;
  }
}

refreshBtn.addEventListener("click", () => currentWiki && load(true));

async function runSearch(q) {
  refreshBtn.hidden = true;
  content.innerHTML = `<div class="state"><div class="spinner"></div>Searching MariaDB for <strong>${esc(q)}</strong>…</div>`;
  try {
    const res = await fetch(`${API_BASE}/api/consistency/search?` + new URLSearchParams({ q }));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();

    const confHtml = (d.conflicts || []).map(c => `
      <div class="card">
        <div class="card-head">
          <span class="badge conflict">${esc(c.conflict_type)}</span>
          <span class="conf">${esc(c.lang_a)}:${esc(c.title_a)} ↔ ${esc(c.lang_b)}:${esc(c.title_b)}</span>
        </div>
        <p class="expl">${esc(c.explanation)}</p>
        <div class="meta-row">
          <a class="pill" href="${esc(fragmentUrl(c.url_a, c.quote_a))}" target="_blank">Highlight in ${esc(c.lang_a)} ↗</a>
          <a class="pill" href="${esc(fragmentUrl(c.url_b, c.quote_b))}" target="_blank">Highlight in ${esc(c.lang_b)} ↗</a>
        </div>
      </div>`).join("");

    const gapHtml = (d.gaps || []).map(g => `
      <div class="card">
        <div class="card-head">
          <span class="badge ${esc(g.importance)}">${esc(g.importance)}</span>
          <span class="conf">${esc(g.src_lang)}:${esc(g.src_title)} → missing in ${esc(g.dst_lang)}</span>
        </div>
        <p class="expl">${esc(g.summary)}</p>
        <a class="pill" href="${esc(fragmentUrl(g.src_url, g.paragraph))}" target="_blank">Highlight in ${esc(g.src_lang)} ↗</a>
      </div>`).join("");

    const artHtml = (d.articles || []).map(a => `
      <div class="card">
        <div class="card-head">
          <a href="${esc(a.url)}" target="_blank" style="font-weight:600;color:#2563eb;text-decoration:none;">
            ${esc(a.lang)}: ${esc(a.title)} ↗
          </a>
          <button class="check-btn" data-lang="${esc(a.lang)}" data-title="${esc(a.title)}">
            ${a.analyzed ? `${a.conflicts_count}c / ${a.gaps_count}g` : "Analyze"}
          </button>
        </div>
        <div class="meta-row"><span>semantic distance: ${a.distance}</span></div>
      </div>`).join("");

    content.innerHTML = `
      <div class="meta-row" style="margin-bottom:6px;">
        <span>Results for “${esc(d.query)}” (${d.took_ms} ms)</span>
        ${currentWiki ? `<a href="#" id="backToPage" style="margin-left:auto;color:#2563eb;">← Back to page</a>` : ""}
      </div>
      ${confHtml ? `<div class="section-label">Matching Conflicts</div>${confHtml}` : ""}
      ${gapHtml ? `<div class="section-label">Matching Missing Content</div>${gapHtml}` : ""}
      ${artHtml ? `<div class="section-label">Semantically Related Articles</div>${artHtml}` : ""}
      ${(!confHtml && !gapHtml && !artHtml) ? `<div class="state">No matches found.</div>` : ""}`;

    content.querySelectorAll(".check-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        currentWiki = { lang: btn.dataset.lang, title: btn.dataset.title };
        load(false);
      });
    });
    const back = document.getElementById("backToPage");
    if (back) {
      back.addEventListener("click", e => {
        e.preventDefault();
        chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
          currentWiki = parseWikiUrl(tabs[0]?.url);
          if (currentWiki) load(false);
        });
      });
    }
  } catch (err) {
    content.innerHTML = `<div class="state">Search failed: ${esc(err.message)}</div>`;
  }
}

searchForm.addEventListener("submit", e => {
  e.preventDefault();
  const q = searchInput.value.trim();
  if (!q) return;
  setMode("check", false);
  runSearch(q);
});

chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
  currentTabId = tabs[0]?.id ?? null;
  currentWiki = parseWikiUrl(tabs[0]?.url);
  if (!currentWiki) return renderNotWiki();
  load(false);
});

// ---------------------------------------------------------------------------------------------
// Modes: Check (above) | Ask (/api/ask on API_BASE) | Drift (opinion drift stream on SITE_BASE)
// ---------------------------------------------------------------------------------------------

let mode = "check";
let driftSource = null;

function stopDrift() {
  if (driftSource) { driftSource.close(); driftSource = null; }
}

function setMode(next, render = true) {
  mode = next;
  stopDrift();
  document.querySelectorAll("#modes .mode").forEach(b => b.classList.toggle("active", b.dataset.mode === next));
  if (next !== "check") refreshBtn.hidden = true;
  if (!render) return;
  if (next === "ask") renderAsk();
  else if (next === "drift") renderDrift();
  else if (currentWiki) load(false);
  else renderNotWiki();
}

document.querySelectorAll("#modes .mode").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));

const pageTitle = () => (currentWiki ? currentWiki.title.replace(/_/g, " ") : "");

// ---- Ask ----

function withCitations(text) {
  return esc(text)
    .replace(/\[(\d+)\]/g, '<a class="cite" href="#cit$1">[$1]</a>')
    .replace(/\n/g, "<br>");
}

function renderAsk() {
  const title = pageTitle();
  content.innerHTML = `
    <form id="askForm" class="ask-form">
      <textarea id="askQ" rows="2" placeholder="Ask about the history of Finland, in English, Finnish or Swedish">${
        title ? esc(`What does Wikipedia say about ${title}?`) : ""}</textarea>
      <div class="ask-row">
        <label title="Answer from Wikipedia as it stood on this date (articles with loaded history)">as of
          <input id="askAsOf" type="date" min="2003-01-01"></label>
        <button type="submit">Ask</button>
      </div>
    </form>
    <div id="askOut"><div class="state">The answer uses only passages found in MariaDB and cites each one.</div></div>`;
  document.getElementById("askForm").addEventListener("submit", e => {
    e.preventDefault();
    const q = document.getElementById("askQ").value.trim();
    if (q) runAsk(q, document.getElementById("askAsOf").value);
  });
}

async function runAsk(question, asOf) {
  const out = document.getElementById("askOut");
  out.innerHTML = `<div class="state"><div class="spinner"></div>Searching MariaDB and writing the answer…<br>
    <small>About 10 to 20 s: retrieval, answer and neutrality review.</small></div>`;
  try {
    const body = { question, mode: "editor" };
    if (asOf) body.as_of = asOf;
    const res = await fetch(`${API_BASE}/api/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const d = await res.json();
    if (!res.ok || d.error) throw new Error(d.error || `HTTP ${res.status}`);
    if (mode !== "ask" || !document.getElementById("askOut")) return;

    const cits = (d.citations || []).map(c => {
      const flagged = c.neutrality && c.neutrality.level === "flagged";
      return `
        <div class="card" id="cit${c.n}">
          <div class="card-head">
            <a href="${esc(c.url)}" target="_blank" class="src-link">[${c.n}] ${esc(c.title)} ↗</a>
            ${flagged ? `<span class="badge conflict">${esc(c.neutrality.category || "wording")}</span>`
                      : `<span class="badge ok">neutral</span>`}
          </div>
          ${flagged ? `<p class="expl">${esc(c.neutrality.reason || "")}</p>` : ""}
          <details><summary>Passage</summary><div class="para">${esc(c.passage)}</div></details>
          ${c.edit_url ? `<div class="meta-row"><a class="pill" href="${esc(c.edit_url)}" target="_blank">Edit article ↗</a></div>` : ""}
        </div>`;
    }).join("");

    const confs = (d.conflicts || []).map(c => `
      <div class="card">
        <div class="card-head"><span class="badge conflict">sources disagree</span></div>
        <p class="expl">${esc(c.about || "")}</p>
        <div class="quotes">
          <div class="quote-box"><div class="quote-label">[${esc(c.a?.n)}]</div><div class="quote-text">“${esc(c.a?.phrase)}”</div></div>
          <div class="quote-box"><div class="quote-label">[${esc(c.b?.n)}]</div><div class="quote-text">“${esc(c.b?.phrase)}”</div></div>
        </div>
      </div>`).join("");

    out.innerHTML = `
      <div class="meta">
        <div class="answer">${withCitations(d.answer || "")}</div>
        <div class="meta-row"><span>${d.took_ms} ms</span>${asOf ? `<span class="pill">as of ${esc(asOf)}</span>` : ""}</div>
      </div>
      ${confs ? `<div class="section-label">Passages that disagree</div>${confs}` : ""}
      ${cits ? `<div class="section-label">Sources cited</div>${cits}` : ""}
      ${d.sql ? `<details><summary>Show MariaDB SQL</summary><pre>${esc(d.sql)}</pre></details>` : ""}`;
  } catch (err) {
    if (document.getElementById("askOut")) {
      out.innerHTML = `<div class="state">Ask failed.<br><small>${esc(err.message)}</small></div>`;
    }
  }
}

// ---- Drift ----

function renderDrift() {
  const title = pageTitle();
  content.innerHTML = `
    <form id="driftForm" class="search-bar drift-form">
      <input id="driftQ" type="text" value="${esc(title)}" placeholder="An article or a question, e.g. Who started the Winter War?">
      <button type="submit">Trace</button>
    </form>
    <div id="driftOut"><div class="state">
      How the Finnish articles' position on the same claims changed over the years, down to the exact edit.
      ${title ? "" : "<br><small>Open a Wikipedia article, or type a question.</small>"}
      <div class="demo-links"><a href="${SITE_BASE}/drift?fixture=1" target="_blank">Watch the recorded demo ↗</a></div>
    </div></div>`;
  document.getElementById("driftForm").addEventListener("submit", e => {
    e.preventDefault();
    const q = document.getElementById("driftQ").value.trim();
    if (q) runDrift(q);
  });
  if (title) runDrift(title);
}

function runDrift(q) {
  stopDrift();
  const out = document.getElementById("driftOut");
  const fullUrl = `${SITE_BASE}/drift?q=${encodeURIComponent(q)}`;
  let articles = [], claims = {}, changes = [], summary = "", status = "Finding the articles…", finished = false;

  const draw = () => {
    if (mode !== "drift" || !document.getElementById("driftOut")) return;
    const arts = articles.map(a =>
      `<a class="pill" href="${esc(a.url)}" target="_blank">${esc(a.title)} · ${a.n_revs} versions</a>`).join("");
    const SHOWN = 12;   // the popup is small: newest changes first, the rest are on the full timeline
    const list = changes
      .slice().sort((x, y) => (x.t < y.t ? 1 : -1)).slice(0, SHOWN)
      .map(c => `
        <div class="card">
          <div class="card-head">
            <span class="badge medium">${esc((c.t || "").slice(0, 7))}</span>
            <a class="conf" href="https://fi.wikipedia.org/w/index.php?diff=${esc(c.rev_after)}&oldid=${esc(c.rev_before)}" target="_blank">the edit ↗</a>
          </div>
          <p class="expl">${esc(c.headline || (claims[c.claim] || {}).label || c.claim)}</p>
          ${c.comment ? `<div class="meta-row"><span>edit summary: “${esc(c.comment)}”</span></div>` : ""}
          ${(c.hunk && (c.hunk.add || []).length) ? `<details><summary>What the edit added</summary><div class="para">${esc(c.hunk.add.slice(0, 2).join("\n\n"))}</div></details>` : ""}
        </div>`).join("");
    out.innerHTML = `
      <div class="meta">
        <div class="meta-title">${finished ? `${changes.length} change${changes.length === 1 ? "" : "s"} of position` : `<span class="spinner inline"></span>${esc(status)}`}</div>
        <div class="meta-row">${arts}</div>
        ${summary ? `<p class="expl" style="margin-top:8px">${esc(summary)}</p>` : ""}
        <div class="meta-row" style="margin-top:8px"><a class="pill open-full" href="${esc(fullUrl)}" target="_blank">Open full timeline ↗</a></div>
      </div>
      ${list || (finished ? `<div class="state">No change of position found on the shared claims.</div>` : "")}
      ${changes.length > SHOWN ? `<div class="state"><a href="${esc(fullUrl)}" target="_blank">${changes.length - SHOWN} earlier changes on the full timeline ↗</a></div>` : ""}`;
  };
  const on = (name, fn) => driftSource.addEventListener(name, e => { fn(JSON.parse(e.data)); draw(); });
  const fail = msg => {
    stopDrift();
    if (!document.getElementById("driftOut")) return;
    out.innerHTML = `<div class="state">${esc(msg)}
      <div class="demo-links"><a href="${esc(fullUrl)}" target="_blank">Open the drift page ↗</a></div></div>`;
  };

  draw();
  // On an article, q is its title and lang its edition: the service then runs on that article alone.
  const onPage = currentWiki && q === pageTitle() ? `&lang=${encodeURIComponent(currentWiki.lang)}` : "";
  driftSource = new EventSource(`${SITE_BASE}/api/drift/stream?q=${encodeURIComponent(q)}${onPage}`);
  on("sources", d => { articles = d.articles || []; status = `Reading ${articles.length} article${articles.length === 1 ? "" : "s"}, writing the claims…`; });
  on("claims", d => { (d.claims || []).forEach(c => { claims[c.id] = c; }); status = `Scoring versions against ${Object.keys(claims).length} claims…`; });
  on("probes", d => { status = `Scoring versions against ${Object.keys(claims).length} claims (round ${(d.round || 0) + 1})…`; });
  on("change", d => { changes.push(d); });
  // The summary text is "ANSWER: ...", then further labelled sections: the popup shows the answer only.
  on("summary", d => { summary = (d.text || "").replace(/^ANSWER:\s*/, "").split(/\n\s*[A-Z][A-Z ]+:/)[0].trim(); });
  on("done", () => { finished = true; stopDrift(); });
  driftSource.addEventListener("problem", e => {
    let msg = e.data;
    try { msg = JSON.parse(e.data); } catch {}
    fail(typeof msg === "string" ? msg : (msg.message || JSON.stringify(msg)));
  });
  driftSource.onerror = () => {
    if (driftSource && !finished) fail(`Could not reach the drift service at ${SITE_BASE}.`);
  };
}
