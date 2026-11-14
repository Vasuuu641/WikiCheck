"""Evaluate search quality on MariaDB's 37 test questions (data/test-questions.tsv).

Run from the repo root:
    .venv/bin/python eval/run_eval.py --label baseline

A question is a HIT at rank r when the r-th result is ANY language version of the expected
article (same wikidata_id as the expected Finnish title). Ranks are raw result positions, like
MariaDB's own evaluate.py, so the numbers are comparable with theirs.

Test questions: MariaDB's mariadb-vector-wikipedia-rag repo (data/test-questions.tsv). Credit them.
Saves results to eval/results/<label>.json so you can compare versions (baseline vs chunks, ...).
"""
import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from api.search import connect, search  # noqa: E402
from api.rewrite import rewrite

DEFAULT_TSV = Path(__file__).resolve().parent / "test-questions.tsv"
TOP_K = 10


def load_questions(path):
    with open(path, encoding="utf-8", newline="") as f:
        # QUOTE_NONE: questions can contain quote characters
        return list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))


def expected_article(cur, fi_title):
    cur.execute("SELECT wikidata_id, page_id FROM article WHERE lang='fi' AND title=%s", (fi_title,))
    return cur.fetchone()  # (wikidata_id, page_id) or None


def is_hit(hit, wikidata_id, fi_page_id):
    if wikidata_id:
        return hit.get("wikidata_id") == wikidata_id
    return hit["lang"] == "fi" and hit["page_id"] == fi_page_id


def summarize(results):
    n = len(results)
    if n == 0:
        return None
    ranks = [r["rank"] for r in results]
    return {
        "n": n,
        "hit@1": sum(1 for x in ranks if x == 1) / n,
        "hit@5": sum(1 for x in ranks if x is not None and x <= 5) / n,
        "hit@10": sum(1 for x in ranks if x is not None and x <= 10) / n,
        "mrr": sum(1 / x for x in ranks if x) / n,
    }


def fmt(name, s):
    if not s:
        return f"{name:<34} (no questions)"
    return (f"{name:<34} n={s['n']:<3} hit@1 {s['hit@1']:>4.0%}  hit@5 {s['hit@5']:>4.0%}  "
            f"hit@10 {s['hit@10']:>4.0%}  MRR {s['mrr']:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--questions", default=str(DEFAULT_TSV))
    ap.add_argument("--misses", action="store_true", help="list every question not found at rank 1")
    ap.add_argument("--rewrite", action="store_true", help="expand each question with an LLM guess first")
    args = ap.parse_args()

    rows = load_questions(args.questions)
    con = connect()
    results = []
    with con.cursor() as cur:
        for row in rows:
            exp = expected_article(cur, row["expected_fi_title"])
            if exp is None:
                print(f"WARNING: expected article not in database: {row['expected_fi_title']!r}")
                continue
            wd, pid = exp
            t0 = time.time()
            q = rewrite(row["question"]) if args.rewrite else row["question"]
            hits, _sql = search(q, TOP_K)
            ms = (time.time() - t0) * 1000
            rank = next((i + 1 for i, h in enumerate(hits) if is_hit(h, wd, pid)), None)
            results.append({
                "lang": row["lang"],
                "question": row["question"],
                "expected": row["expected_fi_title"],
                "only_in_fi": row["article_exists_in"].strip() == "fi",
                "rank": rank,
                "top3": [h["title"] for h in hits[:3]],
                "ms": round(ms),
            })

    print(f"\n== {args.label}: {len(results)} questions ==")
    print(fmt("all", summarize(results)))
    for lang in ("en", "sv", "fi"):
        print(fmt(f"question in {lang}", summarize([r for r in results if r["lang"] == lang])))
    print(fmt("answer exists only in Finnish", summarize([r for r in results if r["only_in_fi"]])))
    # ASSUMPTION: the 5 president questions are the last 5 rows, as in MariaDB's notes.
    print(fmt("first 32 (assumed: no presidents)", summarize(results[:32])))
    if results:
        print(f"median time per question: {statistics.median(r['ms'] for r in results)} ms")

    missed = [r for r in results if r["rank"] is None or r["rank"] > (1 if args.misses else 5)]
    if missed:
        print("\nNot found" + (" at rank 1" if args.misses else " in the top 5") + ":")
        for r in missed:
            print(f"  [{r['lang']}] rank={r['rank']}  expected={r['expected']}  | {r['question'][:70]}")
            print(f"         got: {', '.join(r['top3'])}")

    out = Path(__file__).resolve().parent / "results"
    out.mkdir(exist_ok=True)
    (out / f"{args.label}.json").write_text(
        json.dumps({"label": args.label, "summary": summarize(results), "results": results},
                   ensure_ascii=False, indent=2))
    print(f"\nsaved eval/results/{args.label}.json")


if __name__ == "__main__":
    main()