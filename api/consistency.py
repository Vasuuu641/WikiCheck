"""Cross-language consistency service: paragraph vectors + cached conflicts & missing-content gaps.

Used by GET /api/consistency for the Chrome extension and web UI, and runnable as a batch
warmer:
  .venv/bin/python -m api.consistency --warm 15
"""
import argparse
import json
import threading
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import numpy as np
import requests

from api import decisions
from api.conflicts import examine
from api.llm import CHECK_MODEL, gemini
from api.search import connect, embed_texts as _embed

GAP_THRESH = 0.48
MIN_CHARS = 200
REF_LANGS = ["en", "fi", "sv", "de", "fr", "es", "ru", "ja", "it", "pt", "nl", "pl", "uk", "ar", "zh"]
MAX_SIBLINGS = 3

def split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n+", text or "")]
    return [
        p for p in parts
        if len(p) >= MIN_CHARS and not p.lstrip().startswith(("|", "*", "-", "#", "=="))
    ]


def embed_texts(texts: list[str]) -> np.ndarray:
    """Unit-length bge-m3 vectors of paragraphs (cut at 1,500 characters), 16 per Ollama call."""
    if not texts:
        return np.zeros((0, 1024), dtype="float32")
    out = []
    for i in range(0, len(texts), 16):
        out.extend(_embed([t[:1500] for t in texts[i:i + 16]]))
    m = np.array(out, dtype="float32")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(norms, 1e-12)


def _filter_gaps_llm(src: dict, candidates: list[tuple[float, str]], dst: dict, top_k: int = 3) -> list[dict]:
    """Score candidate missing paragraphs in one Gemini Decisions API pass, then summarize winners."""
    cands = sorted(candidates, key=lambda x: x[0])[:12]
    if not cands:
        return []
    cand_block = "\n\n".join(f"[{i}] (sim={s:.2f}) {p[:1200]}" for i, (s, p) in enumerate(cands))
    shared_input = (
        f"=== CANDIDATE PARAGRAPHS FROM {src['lang']}:{src['title']} ===\n{cand_block}\n\n"
        f"=== TARGET ARTICLE {dst['lang']}:{dst['title']} ===\n{dst['text'][:8000]}"
    )
    questions = {}
    for i in range(len(cands)):
        questions[f"missing_{i}"] = {
            "type": "noul",
            "instructions": (
                f"Is the core factual content of Candidate [{i}] genuinely ABSENT from "
                f"{dst['lang']}:{dst['title']} (not already covered even briefly or in different words, "
                "and not just boilerplate, a list, or trivia)?"
            ),
        }
        questions[f"important_{i}"] = {
            "type": "noul",
            "instructions": (
                f"Is Candidate [{i}] encyclopedic information worth adding to {dst['lang']}:{dst['title']} "
                "(history, geography, notable events, economy, or demographics) rather than trivial detail?"
            ),
        }

    answers = decisions.ask(shared_input, questions)

    winners = []
    for i, (sim, para) in enumerate(cands):
        p_miss = float(answers.get(f"missing_{i}", {}).get("noul", 0.0))
        p_imp = float(answers.get(f"important_{i}", {}).get("noul", 0.0))
        if p_miss < 0.65:
            continue
        imp = "high" if p_imp >= 0.65 else ("medium" if p_imp >= 0.35 else "low")
        if imp == "low":
            continue
        winners.append((0 if imp == "high" else 1, -(p_miss + p_imp), i, sim, imp, para))

    winners.sort()
    winners = winners[:top_k]
    if not winners:
        return []

    # Summarize only the <= top_k winning paragraphs
    win_block = "\n\n".join(f"[{idx}] {para[:1200]}" for _, _, idx, _, _, para in winners)
    sum_prompt = (
        f"Write a concise 1-sentence English summary of what each paragraph below adds that is missing "
        f"from {dst['lang']}:{dst['title']}.\n"
        f"Return ONLY JSON: {{\"summaries\":{{\"0\":\"...\"}}}}\n\n{win_block}"
    )
    txt = gemini(sum_prompt, CHECK_MODEL, timeout=30,
                 config={"temperature": 0, "responseMimeType": "application/json"})
    m = re.search(r"\{.*\}", txt or "", re.S)
    sums = json.loads(m.group(0)).get("summaries", {}) if m else {}

    return [
        {
            "sim": round(sim, 3),
            "importance": imp,
            "summary": sums.get(str(idx)) or para[:180],
            "paragraph": para,
        }
        for _, _, idx, sim, imp, para in winners
    ]


def _ensure_passage_vectors(cur, art: dict) -> tuple[list[str], np.ndarray]:
    cur.execute(
        "SELECT para_idx, text, VEC_ToText(embedding) FROM passage_vector "
        "WHERE lang=%s AND page_id=%s ORDER BY para_idx",
        (art["lang"], art["page_id"]),
    )
    rows = cur.fetchall()
    if rows:
        paras = [r[1] for r in rows]
        emb = np.array([json.loads(r[2]) for r in rows], dtype="float32")
        norms = np.linalg.norm(emb, axis=1, keepdims=True)
        return paras, emb / np.maximum(norms, 1e-12)

    paras = split_paragraphs(art["text"])
    emb = embed_texts(paras)
    for idx, (p, vec) in enumerate(zip(paras, emb)):
        vec_txt = "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
        cur.execute(
            "INSERT IGNORE INTO passage_vector (lang, page_id, para_idx, wikidata_id, text, embedding) "
            "VALUES (%s, %s, %s, %s, %s, VEC_FromText(%s))",
            (art["lang"], art["page_id"], idx, art["wikidata_id"], p, vec_txt),
        )
    return paras, emb


def _load_or_embed_articles(cur, arts: list[dict]) -> None:
    """Load cached passage_vector rows; embed any uncached articles in parallel."""
    missing = []
    for a in arts:
        cur.execute(
            "SELECT para_idx, text, VEC_ToText(embedding) FROM passage_vector "
            "WHERE lang=%s AND page_id=%s ORDER BY para_idx",
            (a["lang"], a["page_id"]),
        )
        rows = cur.fetchall()
        if rows:
            a["paras"] = [r[1] for r in rows]
            emb = np.array([json.loads(r[2]) for r in rows], dtype="float32")
            norms = np.linalg.norm(emb, axis=1, keepdims=True)
            a["emb"] = emb / np.maximum(norms, 1e-12)
        else:
            a["paras"] = split_paragraphs(a["text"])
            missing.append(a)

    if missing:
        with ThreadPoolExecutor(min(4, len(missing))) as ex:
            embs = list(ex.map(lambda x: embed_texts(x["paras"]), missing))
        for a, emb in zip(missing, embs):
            a["emb"] = emb
            for idx, (p, vec) in enumerate(zip(a["paras"], emb)):
                vec_txt = "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
                cur.execute(
                    "INSERT IGNORE INTO passage_vector (lang, page_id, para_idx, wikidata_id, text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, VEC_FromText(%s))",
                    (a["lang"], a["page_id"], idx, a["wikidata_id"], p, vec_txt),
                )


def _pick_top_siblings(arts: list[dict], focus_art: dict) -> list[dict]:
    """Pick up to MAX_SIBLINGS richest sibling editions (prioritizing en + longest text)."""
    sibs = [a for a in arts if a is not focus_art]
    sibs.sort(key=lambda a: (0 if a["lang"] == "en" else 1, -len(a.get("text") or "")))
    return sibs[:MAX_SIBLINGS]


# One analysis per entity at a time. The extension starts an analysis when a page loads, so the
# popup's own request often arrives while it runs: it then waits for that one instead of paying again.
_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()
INFLIGHT_WAIT_S = 180


def analyze_entity(wikidata_id: str, force: bool = False, focus_lang: str | None = None) -> dict:
    """Compute and cache cross-language conflicts + filtered gaps for one Wikidata entity.
    If the same entity is already being analysed, wait for that run and report it as cached."""
    with _inflight_lock:
        running = _inflight.get(wikidata_id)
        if running is None:
            mine = _inflight[wikidata_id] = threading.Event()
    if running is not None:
        running.wait(INFLIGHT_WAIT_S)
        return {"wikidata_id": wikidata_id, "cached": True, "joined": True}
    try:
        return _analyze_entity(wikidata_id, force, focus_lang)
    finally:
        with _inflight_lock:
            _inflight.pop(wikidata_id, None)
        mine.set()


def _analyze_entity(wikidata_id: str, force: bool = False, focus_lang: str | None = None) -> dict:
    t0 = time.time()
    with connect() as con, con.cursor() as cur:
        if not force:
            cur.execute("SELECT 1 FROM consistency_status WHERE wikidata_id=%s", (wikidata_id,))
            if cur.fetchone():
                return {"wikidata_id": wikidata_id, "cached": True}

        cur.execute(
            "SELECT lang, page_id, rev_id, wikidata_id, title, url, text "
            "FROM article WHERE wikidata_id=%s ORDER BY lang",
            (wikidata_id,),
        )
        cols = [c[0] for c in cur.description]
        arts = [dict(zip(cols, r)) for r in cur.fetchall()]
        if len(arts) < 2:
            cur.execute("REPLACE INTO consistency_status (wikidata_id) VALUES (%s)", (wikidata_id,))
            return {"wikidata_id": wikidata_id, "articles": len(arts), "conflicts": 0, "gaps": 0}

        _load_or_embed_articles(cur, arts)

        if force:
            cur.execute("DELETE FROM article_conflict WHERE wikidata_id=%s", (wikidata_id,))
            cur.execute("DELETE FROM article_gap WHERE wikidata_id=%s", (wikidata_id,))

    # Hub-and-spoke O(1) pair selection when focus_lang is given (or > 4 editions exist)
    focus_art = next((a for a in arts if a["lang"] == focus_lang), None) if focus_lang else None
    if focus_art and len(arts) > 4:
        sibs = _pick_top_siblings(arts, focus_art)
        unordered = [(focus_art, s) for s in sibs]
        directed = [(s, focus_art) for s in sibs]
    else:
        capped = arts[:MAX_SIBLINGS + 1]
        unordered = [(capped[i], capped[j]) for i in range(len(capped)) for j in range(i + 1, len(capped))]
        directed = [(x, y) for x in capped for y in capped if x["lang"] != y["lang"]]

    def check_conflicts(pair):
        a, b = pair
        try:
            return a, b, [c for c in examine(a, b, fast=True) if c.get("confirmed")]
        except Exception:
            return a, b, []

    def check_gaps(pair):
        src, dst = pair
        if not len(src["paras"]) or not len(dst["paras"]):
            return src, dst, []
        best = (src["emb"] @ dst["emb"].T).max(axis=1)
        raw = [(float(s), p) for s, p in zip(best, src["paras"]) if s < GAP_THRESH]
        if not raw:
            return src, dst, []
        try:
            return src, dst, _filter_gaps_llm(src, raw, dst)
        except Exception:
            return src, dst, []

    with ThreadPoolExecutor(max(1, min(9, len(unordered) + len(directed)))) as ex:
        conf_futs = [ex.submit(check_conflicts, p) for p in unordered]
        gap_futs = [ex.submit(check_gaps, p) for p in directed]
        conf_res = [f.result() for f in conf_futs]
        gap_res = [f.result() for f in gap_futs]

    n_conf = n_gap = 0
    with connect() as con, con.cursor() as cur:
        for a, b, cs in conf_res:
            for c in cs:
                cur.execute(
                    """INSERT INTO article_conflict
                       (wikidata_id, lang_a, page_id_a, title_a, url_a,
                        lang_b, page_id_b, title_b, url_b,
                        conflict_type, claim_a, quote_a, claim_b, quote_b,
                        explanation, confidence, verifier_reason)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        wikidata_id,
                        a["lang"], a["page_id"], a["title"], a["url"],
                        b["lang"], b["page_id"], b["title"], b["url"],
                        c.get("type", "other"),
                        c.get("claim_a", ""), c.get("quote_a", ""),
                        c.get("claim_b", ""), c.get("quote_b", ""),
                        c.get("explanation", ""), float(c.get("confidence", 0.0)),
                        c.get("verifier", {}).get("reason", ""),
                    ),
                )
                n_conf += 1
        for src, dst, gs in gap_res:
            for g in gs:
                cur.execute(
                    """INSERT INTO article_gap
                       (wikidata_id, src_lang, src_page_id, src_title, src_url,
                        dst_lang, dst_page_id, dst_title, dst_url,
                        importance, sim, summary, paragraph)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        wikidata_id,
                        src["lang"], src["page_id"], src["title"], src["url"],
                        dst["lang"], dst["page_id"], dst["title"], dst["url"],
                        g["importance"], float(g["sim"]), g["summary"], g["paragraph"],
                    ),
                )
                n_gap += 1
        cur.execute("REPLACE INTO consistency_status (wikidata_id) VALUES (%s)", (wikidata_id,))

    return {
        "wikidata_id": wikidata_id,
        "cached": False,
        "articles": len(arts),
        "conflicts": n_conf,
        "gaps": n_gap,
        "took_ms": int((time.time() - t0) * 1000),
    }


CONFLICT_SQL = """
SELECT id, wikidata_id, lang_a, page_id_a, title_a, url_a,
       lang_b, page_id_b, title_b, url_b,
       conflict_type, claim_a, quote_a, claim_b, quote_b,
       explanation, confidence, verifier_reason
FROM article_conflict
WHERE (lang_a=%s AND page_id_a=%s) OR (lang_b=%s AND page_id_b=%s)
ORDER BY confidence DESC, id ASC
"""

GAP_SQL = """
SELECT id, wikidata_id, src_lang, src_page_id, src_title, src_url,
       dst_lang, dst_page_id, dst_title, dst_url,
       importance, sim, summary, paragraph
FROM article_gap
WHERE dst_lang=%s AND dst_page_id=%s
ORDER BY FIELD(importance, 'high', 'medium', 'low'), sim ASC, id ASC
"""


def _fetch_wiki_page(lang: str, title: str) -> dict | None:
    """Fetch plain text, Wikidata ID, and 2-letter langlinks from live Wikipedia API."""
    r = requests.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        headers={"User-Agent": "WikiCheck/0.1 (cross-language consistency)"},
        params={
            "action": "query",
            "titles": title.replace("_", " "),
            "redirects": 1,
            "prop": "pageprops|langlinks|extracts|info",
            "explaintext": 1,
            "lllimit": 500,
            "inprop": "url",
            "format": "json",
        },
        timeout=15,
    )
    if not r.ok:
        return None
    pages = r.json().get("query", {}).get("pages", {})
    page = next(iter(pages.values()), None)
    if not page or "missing" in page:
        return None
    return {
        "lang": lang,
        "page_id": page["pageid"],
        "rev_id": page.get("lastrevid", 0),
        "wikidata_id": page.get("pageprops", {}).get("wikibase_item"),
        "title": page["title"],
        "url": page.get("fullurl") or f"https://{lang}.wikipedia.org/wiki/{quote(page['title'].replace(' ', '_'))}",
        "text": page.get("extract", ""),
        "langlinks": {
            ll["lang"]: ll["*"]
            for ll in page.get("langlinks", [])
            if len(ll.get("lang", "")) == 2 and ll["lang"] != lang
        },
    }


def _select_ref_langs(langlinks: dict[str, str]) -> list[tuple[str, str]]:
    """Pick up to MAX_SIBLINGS reference editions ordered by REF_LANGS priority."""
    order = {l: i for i, l in enumerate(REF_LANGS)}
    ranked = sorted(langlinks.items(), key=lambda kv: (order.get(kv[0], 999), kv[0]))
    return ranked[:MAX_SIBLINGS]


def _ingest_live_entity(lang: str, title: str) -> dict | None:
    """Fetch a live Wikipedia article + top reference siblings in parallel and store in MariaDB."""
    main = _fetch_wiki_page(lang, title)
    if not main:
        return None
    chosen = _select_ref_langs(main["langlinks"])
    pages = [main]
    if chosen:
        with ThreadPoolExecutor(min(MAX_SIBLINGS, len(chosen))) as ex:
            sibs = list(ex.map(lambda kv: _fetch_wiki_page(kv[0], kv[1]), chosen))
        for sib in sibs:
            if sib:
                if not sib["wikidata_id"]:
                    sib["wikidata_id"] = main["wikidata_id"]
                pages.append(sib)

    with connect() as con, con.cursor() as cur:
        for p in pages:
            cur.execute(
                """INSERT INTO article (lang, page_id, rev_id, wikidata_id, title, url, text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                     rev_id=VALUES(rev_id), wikidata_id=COALESCE(VALUES(wikidata_id), wikidata_id),
                     title=VALUES(title), url=VALUES(url), text=VALUES(text)""",
                (p["lang"], p["page_id"], p["rev_id"], p["wikidata_id"], p["title"], p["url"], p["text"]),
            )
    return {k: main[k] for k in ("lang", "page_id", "wikidata_id", "title", "url")}


def invalidate_entity(wikidata_id: str | None = None, lang: str | None = None, title: str | None = None) -> int:
    """Drop cached status + passage vectors so the next request recomputes fresh data."""
    with connect() as con, con.cursor() as cur:
        if not wikidata_id and lang and title:
            cur.execute("SELECT wikidata_id FROM article WHERE lang=%s AND title=%s LIMIT 1",
                        (lang.lower().strip(), title.replace("_", " ").strip()))
            row = cur.fetchone()
            wikidata_id = row[0] if row else None
        if not wikidata_id:
            return 0
        cur.execute("DELETE FROM consistency_status WHERE wikidata_id=%s", (wikidata_id,))
        n = cur.rowcount
        cur.execute("DELETE FROM passage_vector WHERE wikidata_id=%s", (wikidata_id,))
        return n


def get_consistency(
    lang: str,
    title: str | None = None,
    page_id: int | None = None,
    compute_if_missing: bool = True,
    refresh: bool = False,
) -> dict:
    """Return cached (or freshly computed) conflicts and missing-content gaps for an article."""
    t0 = time.time()
    lang = lang.lower().strip()
    art = None
    with connect() as con, con.cursor() as cur:
        if page_id is not None:
            cur.execute(
                "SELECT lang, page_id, wikidata_id, title, url FROM article WHERE lang=%s AND page_id=%s",
                (lang, page_id),
            )
        elif title:
            clean = title.replace("_", " ").strip()
            cur.execute(
                "SELECT lang, page_id, wikidata_id, title, url FROM article "
                "WHERE lang=%s AND (title=%s OR title=%s) LIMIT 1",
                (lang, clean, title.strip()),
            )
        else:
            return {"found": False, "error": "Provide title or page_id"}
        row = cur.fetchone()
        if row:
            art = dict(zip(["lang", "page_id", "wikidata_id", "title", "url"], row))

    if not art and title and compute_if_missing and re.fullmatch(r"[a-z]{2}", lang):
        art = _ingest_live_entity(lang, title)

    if not art:
        return {"found": False, "lang": lang, "title": title, "page_id": page_id,
                "conflicts": [], "gaps": []}

    with connect() as con, con.cursor() as cur:
        if not art["wikidata_id"]:
            return {"found": True, "article": art, "cached": True, "siblings": [],
                    "conflicts": [], "gaps": [], "note": "No Wikidata ID linked for cross-language check"}

        cur.execute(
            "SELECT lang, page_id, title, url FROM article WHERE wikidata_id=%s AND NOT (lang=%s AND page_id=%s)",
            (art["wikidata_id"], art["lang"], art["page_id"]),
        )
        siblings = [dict(zip(["lang", "page_id", "title", "url"], r)) for r in cur.fetchall()]

        cur.execute("SELECT 1 FROM consistency_status WHERE wikidata_id=%s", (art["wikidata_id"],))
        is_cached = cur.fetchone() is not None

    if (not is_cached and compute_if_missing) or refresh:
        analyze_entity(art["wikidata_id"], force=refresh, focus_lang=art["lang"])
        cached_hit = False
    else:
        cached_hit = is_cached

    with connect() as con, con.cursor() as cur:
        cur.execute(CONFLICT_SQL, (art["lang"], art["page_id"], art["lang"], art["page_id"]))
        c_cols = [c[0] for c in cur.description]
        conflicts = []
        for r in cur.fetchall():
            d = dict(zip(c_cols, r))
            # Orient so `current` is always the requested article and `other` is the sibling
            if d["lang_a"] == art["lang"] and d["page_id_a"] == art["page_id"]:
                conflicts.append({
                    "id": d["id"], "type": d["conflict_type"], "confidence": d["confidence"], "current_is_a": True,
                    "explanation": d["explanation"], "verifier_reason": d["verifier_reason"],
                    "current": {"lang": d["lang_a"], "title": d["title_a"], "url": d["url_a"],
                                "claim": d["claim_a"], "quote": d["quote_a"]},
                    "other": {"lang": d["lang_b"], "title": d["title_b"], "url": d["url_b"],
                              "claim": d["claim_b"], "quote": d["quote_b"]},
                })
            else:
                conflicts.append({
                    "id": d["id"], "type": d["conflict_type"], "confidence": d["confidence"], "current_is_a": False,
                    "explanation": d["explanation"], "verifier_reason": d["verifier_reason"],
                    "current": {"lang": d["lang_b"], "title": d["title_b"], "url": d["url_b"],
                                "claim": d["claim_b"], "quote": d["quote_b"]},
                    "other": {"lang": d["lang_a"], "title": d["title_a"], "url": d["url_a"],
                              "claim": d["claim_a"], "quote": d["quote_a"]},
                })

        cur.execute(GAP_SQL, (art["lang"], art["page_id"]))
        g_cols = [c[0] for c in cur.description]
        gaps = []
        for r in cur.fetchall():
            d = dict(zip(g_cols, r))
            gaps.append({
                "id": d["id"], "importance": d["importance"], "sim": d["sim"],
                "summary": d["summary"], "paragraph": d["paragraph"],
                "source": {"lang": d["src_lang"], "title": d["src_title"], "url": d["src_url"]},
            })

    return {
        "found": True,
        "cached": cached_hit,
        "article": art,
        "siblings": siblings,
        "conflicts": conflicts,
        "gaps": gaps,
        "sql": {"conflicts": CONFLICT_SQL.strip(), "gaps": GAP_SQL.strip()},
        "took_ms": int((time.time() - t0) * 1000),
    }


SEMANTIC_ARTICLES_SQL = """
SELECT a.lang, a.page_id, a.wikidata_id, a.title, a.url, v.distance
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(%s)) AS distance
      FROM article_vector ORDER BY distance LIMIT %s) v
JOIN article a USING (lang, page_id)
ORDER BY v.distance
"""


def search_consistency(q: str, limit: int = 8) -> dict:
    """Semantic + keyword search across articles, cached conflicts, and cached gaps."""
    t0 = time.time()
    q = (q or "").strip()
    if not q:
        return {"query": q, "articles": [], "conflicts": [], "gaps": [], "took_ms": 0}

    vec = embed_texts([q])[0]
    vec_txt = "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
    like = f"%{q}%"

    with connect() as con, con.cursor() as cur:
        cur.execute("SET mhnsw_ef_search = 80")
        cur.execute(SEMANTIC_ARTICLES_SQL, (vec_txt, limit))
        a_cols = [c[0] for c in cur.description]
        articles = [dict(zip(a_cols, r)) for r in cur.fetchall()]
        for a in articles:
            a["distance"] = round(float(a["distance"]), 4)
            if a["wikidata_id"]:
                cur.execute(
                    "SELECT (SELECT COUNT(*) FROM article_conflict WHERE wikidata_id=%s),"
                    "       (SELECT COUNT(*) FROM article_gap WHERE wikidata_id=%s),"
                    "       EXISTS(SELECT 1 FROM consistency_status WHERE wikidata_id=%s)",
                    (a["wikidata_id"], a["wikidata_id"], a["wikidata_id"]),
                )
                nc, ng, analyzed = cur.fetchone()
                a["conflicts_count"] = nc
                a["gaps_count"] = ng
                a["analyzed"] = bool(analyzed)
            else:
                a["conflicts_count"] = a["gaps_count"] = 0
                a["analyzed"] = False

        cur.execute(
            """SELECT id, conflict_type, explanation, confidence,
                      lang_a, title_a, url_a, quote_a,
                      lang_b, title_b, url_b, quote_b
               FROM article_conflict
               WHERE explanation LIKE %s OR claim_a LIKE %s OR claim_b LIKE %s OR title_a LIKE %s OR title_b LIKE %s
               ORDER BY confidence DESC LIMIT %s""",
            (like, like, like, like, like, limit),
        )
        c_cols = [c[0] for c in cur.description]
        conflicts = [dict(zip(c_cols, r)) for r in cur.fetchall()]

        cur.execute(
            """SELECT id, importance, summary, paragraph,
                      src_lang, src_title, src_url, dst_lang, dst_title, dst_url
               FROM article_gap
               WHERE summary LIKE %s OR paragraph LIKE %s OR src_title LIKE %s OR dst_title LIKE %s
               ORDER BY FIELD(importance, 'high', 'medium', 'low'), sim ASC LIMIT %s""",
            (like, like, like, like, limit),
        )
        g_cols = [c[0] for c in cur.description]
        gaps = [dict(zip(g_cols, r)) for r in cur.fetchall()]

    return {
        "query": q,
        "articles": articles,
        "conflicts": conflicts,
        "gaps": gaps,
        "sql": SEMANTIC_ARTICLES_SQL.strip(),
        "took_ms": int((time.time() - t0) * 1000),
    }


def warm_cache(limit: int = 10, seed: int = 5):
    with connect() as con, con.cursor() as cur:
        cur.execute(
            """SELECT a.wikidata_id FROM article a
               LEFT JOIN consistency_status cs USING (wikidata_id)
               WHERE a.wikidata_id IS NOT NULL AND cs.wikidata_id IS NULL
               GROUP BY a.wikidata_id HAVING COUNT(DISTINCT a.lang) >= 2
               ORDER BY RAND(%s) LIMIT %s""",
            (seed, limit),
        )
        wids = [r[0] for r in cur.fetchall()]
    print(f"Warming {len(wids)} entities...", flush=True)
    for wid in wids:
        res = analyze_entity(wid)
        print(res, flush=True)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", type=int, default=0, help="Precompute N entities into MariaDB cache")
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args()
    if args.warm:
        warm_cache(args.warm, args.seed)
