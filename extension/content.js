(() => {
  const STYLE_ID = "aikakone-highlight-style";
  if (!document.getElementById(STYLE_ID)) {
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      .aikakone-hl {
        outline: 3px solid #dc2626 !important;
        background-color: #fef08a !important;
        border-radius: 4px;
        transition: background-color 0.3s ease;
      }
    `;
    document.head.appendChild(style);
  }

  function norm(s) {
    return (s || "")
      .replace(/\[\d+\]/g, " ")
      .replace(/[|#*`]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function findElement(query) {
    const root = document.getElementById("mw-content-text") || document.body;
    const target = norm(query);
    if (!target) return null;

    // Try full normalized string, then shorter prefixes/segments (useful for markdown table rows)
    const parts = query
      .split("|")
      .map(norm)
      .filter(p => p.length >= 6)
      .sort((a, b) => b.length - a.length);
    const needles = [target, ...parts, target.slice(0, 60), target.slice(0, 30)].filter(n => n.length >= 6);

    const blocks = Array.from(root.querySelectorAll("p, td, th, li, blockquote, tr"));
    for (const needle of needles) {
      for (const el of blocks) {
        if (norm(el.innerText).includes(needle)) return el;
      }
    }
    return null;
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.action !== "highlight") return;
    document.querySelectorAll(".aikakone-hl").forEach(el => el.classList.remove("aikakone-hl"));
    const el = findElement(msg.text);
    if (el) {
      el.classList.add("aikakone-hl");
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false });
    }
  });
})();
