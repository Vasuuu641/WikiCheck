"""Experiment: missing-content finder across language editions of the same Wikidata entity.

Split both articles into paragraphs, embed each (bge-m3 via Ollama), and flag paragraphs of
article A that have no semantically close paragraph in article B (cosine < THRESH).
No LLM involved in detection. --describe adds a short LLM description of the top gaps.
  .venv/bin/python -m eval.experiments.gaps --n 40
"""
import argparse
import json
import os
import random
import re
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

load_dotenv()
from api.search import connect  # noqa: E402

THRESH = 0.55
MIN_CHARS = 200


def paragraphs(text):
    parts = [p.strip() for p in re.split(r"\n+", text)]
    return [p for p in parts if len(p) >= MIN_CHARS and not p.startswith("|")]


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


def gaps_from_emb(ea, pa, eb):
    if not len(pa) or not len(eb):
        return []
    best = (ea @ eb.T).max(axis=1)
    return [(float(s), p) for s, p in zip(best, pa) if s < THRESH]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args()
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
                arts.append({"lang": lang, "title": title, "paras": ps,
                             "emb": embed(ps) if ps else np.zeros((0, 1024), "float32")})
            for x in arts:
                for y in arts:
                    if x["lang"] == y["lang"]:
                        continue
                    g = gaps_from_emb(x["emb"], x["paras"], y["emb"])
                    f.write(json.dumps({"wikidata_id": wid, "from": [x["lang"], x["title"]],
                                        "missing_in": [y["lang"], y["title"]],
                                        "paras_from": len(x["paras"]), "paras_other": len(y["paras"]),
                                        "unmatched": g}, ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"{x['lang']}:{x['title']} -> {y['lang']}: {len(g)}/{len(x['paras'])} "
                          f"paragraphs unmatched", flush=True)
    print("done")


if __name__ == "__main__":
    main()
