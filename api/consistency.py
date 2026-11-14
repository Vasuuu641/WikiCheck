"""Cross-language consistency service: paragraph vectors + cached conflicts & missing-content gaps.

Used by GET /api/consistency for the Chrome extension and web UI, and runnable as a batch
warmer:
  .venv/bin/python -m api.consistency --warm 15
"""
import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from api.conflicts import examine
from api.search import connect

GAP_THRESH = 0.48
MIN_CHARS = 200
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

GAP_FILTER_PROMPT = """You are a Wikipedia editor comparing two language editions of the same topic.
Below are candidate paragraphs from {src_lang}:{src_title} that had low embedding similarity
against {dst_lang}:{dst_title}, followed by the full text of {dst_lang}:{dst_title}.

For each candidate paragraph [idx]:
1. Is its core factual content genuinely ABSENT from {dst_lang}:{dst_title}?
   Mark truly_missing=false if {dst_lang}:{dst_title} already covers the same facts (even briefly or in different words),
   or if the paragraph is just boilerplate, a list, or trivia.
2. Rate encyclopedic importance for a reader/editor of {dst_lang}: "high" (major historical event, key bio milestone,
   significant statistics, whole missing topic/section), "medium" (useful supporting detail), or "low" (minor local color/trivia).
3. Write a 1-sentence English summary of what is missing (empty string if truly_missing=false).

Return ONLY JSON: {{"items":[{{"idx":0,"truly_missing":true,"importance":"high","summary":"..."}}]}}

=== CANDIDATE PARAGRAPHS FROM {src_lang}:{src_title} ===
{candidates}

=== TARGET ARTICLE {dst_lang}:{dst_title} ===
{dst_text}"""


def split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n+", text or "")]
    return [
        p for p in parts
        if len(p) >= MIN_CHARS and not p.lstrip().startswith(("|", "*", "-", "#", "=="))
    ]


def embed_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 1024), dtype="float32")
    out = []
    url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/embed"
    model = os.getenv("EMBED_MODEL", "bge-m3")
    for i in range(0, len(texts), 16):
        r = requests.post(
            url,
            json={"model": model, "input": [t[:1500] for t in texts[i:i + 16]]},
            timeout=300,
        )
        r.raise_for_status()
        out.extend(r.json()["embeddings"])
    m = np.array(out, dtype="float32")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(norms, 1e-12)


def _filter_gaps_llm(src: dict, candidates: list[tuple[float, str]], dst: dict, top_k: int = 3) -> list[dict]:
    cands = sorted(candidates, key=lambda x: x[0])[:12]
    if not cands:
        return []
    cand_block = "\n\n".join(f"[{i}] (sim={s:.2f}) {p[:1200]}" for i, (s, p) in enumerate(cands))
    prompt = GAP_FILTER_PROMPT.format(
        src_lang=src["lang"], src_title=src["title"],
        dst_lang=dst["lang"], dst_title=dst["title"],
        candidates=cand_block, dst_text=dst["text"][:8000],
    )
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
        timeout=120,
    )
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    m = re.search(r"\{.*\}", txt or "", re.S)
    items = json.loads(m.group(0)).get("items", []) if m else []
    rank = {"high": 0, "medium": 1, "low": 2}
    kept = []
    for it in items:
        idx = it.get("idx")
        imp = it.get("importance", "low")
        if not it.get("truly_missing") or imp not in ("high", "medium"):
            continue
        if isinstance(idx, int) and 0 <= idx < len(cands):
            sim, para = cands[idx]
            kept.append({"sim": round(sim, 3), "importance": imp,
                         "summary": it.get("summary", ""), "paragraph": para})
    kept.sort(key=lambda x: (rank.get(x["importance"], 2), x["sim"]))
    return kept[:top_k]


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


def analyze_entity(wikidata_id: str, force: bool = False) -> dict:
    """Compute and cache cross-language conflicts + filtered gaps for one Wikidata entity."""
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

        for a in arts:
            a["paras"], a["emb"] = _ensure_passage_vectors(cur, a)

        if force:
            cur.execute("DELETE FROM article_conflict WHERE wikidata_id=%s", (wikidata_id,))
            cur.execute("DELETE FROM article_gap WHERE wikidata_id=%s", (wikidata_id,))

    # Run LLM checks outside the DB transaction so we don't hold connections open
    unordered = [(arts[i], arts[j]) for i in range(len(arts)) for j in range(i + 1, len(arts))]
    directed = [(x, y) for x in arts for y in arts if x["lang"] != y["lang"]]

    def check_conflicts(pair):
        a, b = pair
        try:
            return a, b, [c for c in examine(a, b) if c.get("confirmed")]
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

    with ThreadPoolExecutor(max(1, min(6, len(unordered) + len(directed)))) as ex:
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
        if not row:
            return {"found": False, "lang": lang, "title": title, "page_id": page_id,
                    "conflicts": [], "gaps": []}
        art = dict(zip(["lang", "page_id", "wikidata_id", "title", "url"], row))
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
        analyze_entity(art["wikidata_id"], force=refresh)
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
                    "id": d["id"], "type": d["conflict_type"], "confidence": d["confidence"],
                    "explanation": d["explanation"], "verifier_reason": d["verifier_reason"],
                    "current": {"lang": d["lang_a"], "title": d["title_a"], "url": d["url_a"],
                                "claim": d["claim_a"], "quote": d["quote_a"]},
                    "other": {"lang": d["lang_b"], "title": d["title_b"], "url": d["url_b"],
                              "claim": d["claim_b"], "quote": d["quote_b"]},
                })
            else:
                conflicts.append({
                    "id": d["id"], "type": d["conflict_type"], "confidence": d["confidence"],
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
