"""Search quality on MariaDB's test questions, as in their evaluate.py: hit@1, hit@5, hit@10, MRR.

    python eval/run_eval.py                          # article-first (MariaDB's default search)
    python eval/run_eval.py --modes article,chunk    # both, side by side, and what changed
    python eval/run_eval.py --runs 3 --misses        # latency over 3 runs; list the misses

Modes:
  article  one vector per article (article_vector), api/search.py search(): MariaDB's setup.
  chunk    one vector per paragraph (chunk_vector, eval/build_chunks.py): the 40 nearest
           paragraphs, an article ranks where its best paragraph ranks.

A hit is the expected article in ANY language: the fi, sv or en version of the same topic
(same wikidata_id) all count, as in MariaDB's evaluate.py. MRR: 1 for first place, 1/2 for
second, ... 0 if not in the top 10. The questions file has 37 rows: the first 32 are MariaDB's
original set, the last 5 are the president questions they added.
"""
import argparse
import csv
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from api.search import (articles_from_chunks, connect, embed_question, embed_texts,  # noqa: E402
                        fused_search, search, search_chunks)

QUESTIONS_URL = ("https://raw.githubusercontent.com/MariaDB/mariadb-vector-wikipedia-rag/"
                 "HEAD/data/test-questions.tsv")
CHUNK_CANDIDATES = 40
MODE_NAMES = {"article": "article-first", "chunk": "chunk-first", "fusion": "fusion (RRF)",
              "rewrite": "article + rewrite"}
REWRITE_MS: list = []   # the rewrite call per question (first run only: later runs hit its cache)


def load_questions(path: Path) -> list[dict]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {QUESTIONS_URL} -> {path}")
        urllib.request.urlretrieve(QUESTIONS_URL, path)
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def rank_of_answer(q: dict, rows: list[dict]) -> int | None:
    for i, r in enumerate(rows, 1):
        if (q["wikidata_id"] and r["wikidata_id"] == q["wikidata_id"]) or \
           (r["lang"] == "fi" and r["page_id"] == q["page_id"]):
            return i
    return None


def top10(mode: str, question: str, vector: list[float]) -> list[dict]:
    if mode == "article":
        return search(question, limit=10, vector=vector)[0]
    if mode == "fusion":
        return fused_search(vector, limit=10)[0]
    if mode == "rewrite":  # QUERY_REWRITE=1 with article-first: the API's own search_articles
        from api.ask import search_articles
        from api.rewrite import rewrite_queries
        rw = rewrite_queries(question)
        if not rw["cached"]:
            REWRITE_MS.append(rw["ms"])
        query_vecs = embed_texts(rw["queries"]) if rw["queries"] else []
        return search_articles(question, vector, query_vecs, "article", limit=10)[0]
    return articles_from_chunks(search_chunks(vector, limit=CHUNK_CANDIDATES)[0])[:10]


def groups(questions):
    yield "all 37" if len(questions) == 37 else f"all {len(questions)}", questions
    for lang, name in (("en", "English"), ("sv", "Swedish"), ("fi", "Finnish")):
        yield f"in {name}", [q for q in questions if q["lang"] == lang]
    yield "answer only in Finnish", [q for q in questions if q["article_exists_in"] == "fi"]
    if len(questions) == 37:
        yield "the first 32 (no presidents)", questions[:32]
        yield "the 5 president questions", questions[32:]


def scores(ranks: list) -> tuple:
    hit = lambda k: sum(1 for r in ranks if r and r <= k) / len(ranks)
    return hit(1), hit(5), hit(10), sum(1 / r for r in ranks if r) / len(ranks)


def run(mode: str, questions: list[dict], vectors: list, runs: int) -> dict:
    """{question index: rank}, plus the search latencies in ms."""
    ranks, ms = {}, []
    for i, q in enumerate(questions):
        for _ in range(runs):
            t = time.perf_counter()
            rows = top10(mode, q["question"], vectors[i])
            ms.append((time.perf_counter() - t) * 1000)
        ranks[i] = (rank_of_answer(q, rows), rows[0]["title"] if rows else "-")
    return {"ranks": ranks, "ms": ms}


def table(questions, results: dict):
    modes = list(results)
    head = "| Questions | n | " + " | ".join(
        f"{MODE_NAMES[m] if len(modes) > 1 else ''} hit@1 | hit@5 | hit@10 | MRR".strip() for m in modes) + " |"
    print(head)
    print("|---|---|" + "---|---|---|---|" * len(modes))
    for label, group in groups(questions):
        if not group:
            continue
        idx = [questions.index(q) for q in group]
        cells = []
        for m in modes:
            h1, h5, h10, mrr = scores([results[m]["ranks"][i][0] for i in idx])
            cells.append(f"{h1:.0%} | {h5:.0%} | {h10:.0%} | {mrr:.2f}")
        print(f"| {label} | {len(group)} | " + " | ".join(cells) + " |")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--questions", type=Path, default=ROOT / "eval" / "test-questions.tsv")
    p.add_argument("--modes", default="article", help="comma-separated: article, chunk, fusion")
    p.add_argument("--runs", type=int, default=1, help="runs per question, for the latency")
    p.add_argument("--misses", action="store_true", help="list questions not in the top 5")
    args = p.parse_args()
    modes = args.modes.split(",")

    questions = load_questions(args.questions)
    with connect() as con, con.cursor() as cur:
        for q in questions:
            cur.execute("SELECT wikidata_id, page_id FROM article WHERE lang = 'fi' AND title = %s",
                        (q["expected_fi_title"],))
            row = cur.fetchone()
            if not row:
                print(f"warning: expected article not in DB: {q['expected_fi_title']}")
            q["wikidata_id"], q["page_id"] = row or (None, None)
        if "chunk" in modes or "fusion" in modes:
            cur.execute("SELECT (SELECT COUNT(DISTINCT page_id) FROM ask_chunk), "
                        "(SELECT COUNT(*) FROM article WHERE lang = 'fi')")
            chunked, fi = cur.fetchone()
            note = "" if chunked >= fi else "  (NOT finished: chunk-first is incomplete)"
            print(f"chunked: {chunked} of {fi} Finnish articles{note}")

    # The question vector is the same for both modes: embed once per run, time it separately.
    embed_ms, vectors = [], []
    for q in questions:
        for _ in range(args.runs):
            t = time.perf_counter()
            v = embed_question(q["question"])
            embed_ms.append((time.perf_counter() - t) * 1000)
        vectors.append(v)

    results = {m: run(m, questions, vectors, args.runs) for m in modes}

    print(f"\n{len(questions)} questions, {args.runs} run(s) each\n")
    table(questions, results)
    print(f"\nLatency, median (slowest): embedding the question {statistics.median(embed_ms):.0f} ms "
          f"({max(embed_ms):.0f})")
    for m in modes:
        ms = results[m]["ms"]
        print(f"  {MODE_NAMES[m]} search (SQL + Python): {statistics.median(ms):.1f} ms ({max(ms):.1f})")
    if REWRITE_MS:
        print(f"  of which the rewrite call (first run, not cached): median {statistics.median(REWRITE_MS):.0f} ms "
              f"({max(REWRITE_MS):.0f}); runs 2-5 use its cache")

    for other in modes[1:]:   # each mode against the first one
        a, b = results[modes[0]]["ranks"], results[other]["ranks"]
        print(f"\nChanged at top 5 ({MODE_NAMES[modes[0]]} -> {MODE_NAMES[other]}):")
        changed = 0
        for i, q in enumerate(questions):
            ra, rb = a[i][0], b[i][0]
            hit_a, hit_b = bool(ra and ra <= 5), bool(rb and rb <= 5)
            if hit_a != hit_b:
                changed += 1
                print(f"- {'miss -> HIT ' if hit_b else 'hit -> MISS '} [{q['lang']}] {q['question']} "
                      f"(expected {q['expected_fi_title']}): rank {ra or '>10'} -> {rb or '>10'}")
        if not changed:
            print("- none")
        print(f"\nOther rank changes, {MODE_NAMES[other]} (still hit or still miss at top 5):")
        for i, q in enumerate(questions):
            ra, rb = a[i][0], b[i][0]
            if ra != rb and bool(ra and ra <= 5) == bool(rb and rb <= 5):
                print(f"- [{q['lang']}] {q['question']}: rank {ra or '>10'} -> {rb or '>10'}")

    if args.misses:
        for m in modes:
            print(f"\nNot in the top 5 ({MODE_NAMES[m]}):")
            for i, q in enumerate(questions):
                rank, top = results[m]["ranks"][i]
                if not rank or rank > 5:
                    print(f"- [{q['lang']}] {q['question']} -> expected {q['expected_fi_title']}, "
                          f"rank {rank or '>10'}, top: {top}")


if __name__ == "__main__":
    main()
