const API_BASE = "http://127.0.0.1:8020";

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

async function updateBadge(tabId, url) {
  const wiki = parseWikiUrl(url);
  if (!wiki) {
    chrome.action.setBadgeText({ tabId, text: "" });
    return;
  }
  try {
    const qs = new URLSearchParams({ lang: wiki.lang, title: wiki.title, compute: "false" });
    const res = await fetch(`${API_BASE}/api/consistency?${qs}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.found || !data.cached) {
      chrome.action.setBadgeText({ tabId, text: "?" });
      chrome.action.setBadgeBackgroundColor({ tabId, color: "#64748b" });
      return;
    }
    const nConf = data.conflicts?.length || 0;
    const nGap = data.gaps?.length || 0;
    const total = nConf + nGap;
    chrome.action.setBadgeText({ tabId, text: String(total) });
    chrome.action.setBadgeBackgroundColor({
      tabId,
      color: nConf > 0 ? "#dc2626" : nGap > 0 ? "#d97706" : "#16a34a"
    });
  } catch {
    chrome.action.setBadgeText({ tabId, text: "" });
  }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url) {
    updateBadge(tabId, tab.url);
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab?.url) updateBadge(tabId, tab.url);
  } catch {}
});
