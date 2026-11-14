const API_BASE = "http://127.0.0.1:8020";
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
  if (q) runSearch(q);
});

chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
  currentTabId = tabs[0]?.id ?? null;
  currentWiki = parseWikiUrl(tabs[0]?.url);
  if (!currentWiki) return renderNotWiki();
  load(false);
});
