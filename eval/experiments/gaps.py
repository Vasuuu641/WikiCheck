"""Experiment: missing-content finder across language editions of the same Wikidata entity.

Split both articles into paragraphs, embed each (bge-m3 via Ollama), flag paragraphs of
article A with cosine < THRESH in article B, then filter with Gemini to keep only genuinely
missing, high-value encyclopedic facts (top-k per pair).

  .venv/bin/python -m eval.experiments.gaps --n 20
  .venv/bin/python -m eval.experiments.gaps --filter-existing eval/results/gaps.jsonl
"""
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

load_dotenv()
from api.search import connect  # noqa: E402

THRESH = 0.48
MIN_CHARS = 200
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

FILTER_PROMPT = """You are a Wikipedia editor comparing two language editions of the same topic.
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


def paragraphs(text):
    parts = [p.strip() for p in re.split(r"\n+", text)]
    return [
        p for p in parts
        if len(p) >= MIN_CHARS and not p.lstrip().startswith(("|", "*", "-", "#", "=="))
    ]


def embed(texts):
    out = []
    for i in range(0, len(texts), 16):
        r = requests.post(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/embed",
                          json={"model": os.getenv("EMBED_MODEL", "bge-m3"),
                                "input": [t[:1500] for t in texts[i:i + 16]]}, timeout=300)
        r.raise_for_status()
        out += r.json()["embeddings"]
    m = np.array(out, dtype="float32")
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def gaps_from_emb(ea, pa, eb, thresh=THRESH):
    if not len(pa) or not len(eb):
        return []
    best = (ea @ eb.T).max(axis=1)
    return [(float(s), p) for s, p in zip(best, pa) if s < thresh]


def filter_gaps(src_lang, src_title, candidates, dst_lang, dst_title, dst_text,
                thresh=THRESH, top_k=3):
    """Filter raw vector-unmatched paragraphs using threshold + Gemini false-gap & importance check."""
    cands = sorted([(s, p) for s, p in candidates
                    if s < thresh and not p.lstrip().startswith(("|", "*", "-", "#", "=="))],
                   key=lambda x: x[0])[:12]
    if not cands:
        return []
    cand_block = "\n\n".join(f"[{i}] (sim={s:.2f}) {p[:1200]}" for i, (s, p) in enumerate(cands))
    prompt = FILTER_PROMPT.format(
        src_lang=src_lang, src_title=src_title, dst_lang=dst_lang, dst_title=dst_title,
        candidates=cand_block, dst_text=dst_text[:8000],
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


def run_filter_existing(src_path: Path, thresh: float, top_k: int):
    rows = [json.loads(l) for l in open(src_path)]
    texts = {}
    with connect() as con, con.cursor() as cur:
        keys = {(r["missing_in"][0], r["missing_in"][1]) for r in rows}
        for lang, title in keys:
            cur.execute("SELECT text FROM article WHERE lang=%s AND title=%s LIMIT 1", (lang, title))
            row = cur.fetchone()
            texts[(lang, title)] = row[0] if row else ""

    def work(r):
        dst_text = texts.get((r["missing_in"][0], r["missing_in"][1]), "")
        try:
            kept = filter_gaps(r["from"][0], r["from"][1], r["unmatched"],
                               r["missing_in"][0], r["missing_in"][1], dst_text,
                               thresh=thresh, top_k=top_k)
        except Exception as e:
            print("error", r["from"], r["missing_in"], e, flush=True)
            kept = []
        return r, kept

    out_path = src_path.parent / "gaps_filtered.jsonl"
    raw_total = sum(len(r["unmatched"]) for r in rows)
    kept_total = pairs_with = 0
    with open(out_path, "w") as f, ThreadPoolExecutor(6) as ex:
        for r, kept in ex.map(work, rows):
            kept_total += len(kept)
            pairs_with += bool(kept)
            out = {"wikidata_id": r["wikidata_id"], "from": r["from"], "missing_in": r["missing_in"],
                   "raw_unmatched": len(r["unmatched"]), "gaps": kept}
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
            if kept:
                print(f"{r['from'][0]}:{r['from'][1]} -> {r['missing_in'][0]}: "
                      f"{len(kept)}/{len(r['unmatched'])} kept", flush=True)
    print(f"\n{len(rows)} directed pairs: {pairs_with} with gaps, {kept_total}/{raw_total} paragraphs kept -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--thresh", type=float, default=THRESH)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--filter-existing")
    args = ap.parse_args()
    if args.filter_existing:
        return run_filter_existing(Path(args.filter_existing), args.thresh, args.top)

    p = Path(__file__).resolve().parents[1] / "results" / "gaps.jsonl"
    with connect() as con, con.cursor() as cur, open(p, "w") as f:
        cur.execute("""SELECT wikidata_id FROM article WHERE wikidata_id IS NOT NULL
                       GROUP BY wikidata_id HAVING COUNT(DISTINCT lang) >= 2
                       ORDER BY RAND(%s) LIMIT %s""", (args.seed, args.n))
        ids = [r[0] for r in cur.fetchall()]
        for wid in ids:
            cur.execute("SELECT lang, title, url, text FROM article WHERE wikidata_id=%s", (wid,))
            arts = []
            for lang, title, url, text in cur.fetchall():
                ps = paragraphs(text)
                arts.append({"lang": lang, "title": title, "text": text, "paras": ps,
                             "emb": embed(ps) if ps else np.zeros((0, 1024), "float32")})
            for x in arts:
                for y in arts:
                    if x["lang"] == y["lang"]:
                        continue
                    raw = gaps_from_emb(x["emb"], x["paras"], y["emb"], thresh=args.thresh)
                    kept = filter_gaps(x["lang"], x["title"], raw, y["lang"], y["title"],
                                       y["text"], thresh=args.thresh, top_k=args.top)
                    f.write(json.dumps({"wikidata_id": wid, "from": [x["lang"], x["title"]],
                                        "missing_in": [y["lang"], y["title"]],
                                        "paras_from": len(x["paras"]), "paras_other": len(y["paras"]),
                                        "unmatched": raw, "gaps": kept}, ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"{x['lang']}:{x['title']} -> {y['lang']}: {len(kept)}/{len(raw)} kept",
                          flush=True)
    print("done")


if __name__ == "__main__":
    main()
