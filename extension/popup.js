const API_BASE = "http://127.0.0.1:8020";
const content = document.getElementById("content");
const refreshBtn = document.getElementById("refreshBtn");

let activeTab = "conflicts";
let currentWiki = null;

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
    const m = u.hostname.match(/^(en|fi|sv)\.wikipedia\.org$/);
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
      Open an article on <strong>en / fi / sv Wikipedia</strong> to check cross-language conflicts and missing content.
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
      ? confs.map(c => `
        <div class="card">
          <div class="card-head">
            <span class="badge conflict">${esc(c.type)}</span>
            <span class="conf">conf ${Math.round((c.confidence || 0) * 100)}%</span>
          </div>
          <p class="expl">${esc(c.explanation)}</p>
          <div class="quotes">
            <div class="quote-box">
              <div class="quote-label">This page (${esc(c.current.lang)})</div>
              <div class="quote-text">“${esc(c.current.quote)}”</div>
            </div>
            <div class="quote-box">
              <div class="quote-label">
                <a href="${esc(c.other.url)}" target="_blank">${esc(c.other.lang)}: ${esc(c.other.title)} ↗</a>
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
            <a class="pill" href="${esc(g.source.url)}" target="_blank">From ${esc(g.source.lang)}: ${esc(g.source.title)} ↗</a>
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

chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
  currentWiki = parseWikiUrl(tabs[0]?.url);
  if (!currentWiki) return renderNotWiki();
  load(false);
});
