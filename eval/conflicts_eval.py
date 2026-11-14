"""Run the conflict pipeline on a sample of pairs and dump everything for human review.

  python -m eval.conflicts_eval --n 20 --kind crosslang --label cl1
Output: eval/results/conflicts_<label>.jsonl  (one line per pair, with `human_label` to fill in)
Metrics printed: how many pairs had conflicts, quote-grounding rate, verifier agreement.
Precision needs the human labels: fill `human_label` (true/false) per conflict, then
  python -m eval.conflicts_eval --score eval/results/conflicts_<label>.jsonl
"""
import argparse
import itertools
import json
import random
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
from api.conflicts import crosslang_pairs, examine, related_pairs, _rows  # noqa: E402


def score(path):
    cs = [c for line in open(path) for c in json.loads(line)["conflicts"]]
    lab = [c for c in cs if c.get("human_label") is not None]
    print(f"{len(cs)} conflicts proposed, {len(lab)} human-labelled")
    for name, sel in [("all proposed", lab), ("confirmed (quotes + verifier)",
                                              [c for c in lab if c["confirmed"]])]:
        if sel:
            p = sum(bool(c["human_label"]) for c in sel) / len(sel)
            print(f"precision[{name}] = {p:.2f} (n={len(sel)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--kind", choices=["crosslang", "related"], default="crosslang")
    ap.add_argument("--label", default="run")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--score")
    args = ap.parse_args()
    if args.score:
        return score(args.score)

    random.seed(args.seed)
    if args.kind == "crosslang":
        pairs = list(itertools.islice(crosslang_pairs(limit=args.n * 3, offset=random.randint(0, 2000)),
                                      args.n * 3))
    else:
        seeds = _rows("SELECT lang, page_id FROM article WHERE lang='en' ORDER BY RAND(%s) LIMIT %s",
                      (args.seed, args.n))
        pairs = [p for s in seeds for p in itertools.islice(related_pairs(s["lang"], s["page_id"]), 2)]
    random.shuffle(pairs)
    pairs = pairs[:args.n]

    out = Path(__file__).resolve().parent / "results" / f"conflicts_{args.label}.jsonl"
    stats = dict(pairs=0, proposed=0, grounded=0, confirmed=0)
    from concurrent.futures import ThreadPoolExecutor

    def work(p):
        try:
            return p, examine(p[0], p[1])
        except Exception as e:  # keep going on API errors
            print("error", p[0]["title"], p[1]["title"], e, flush=True)
            return p, None

    with open(out, "w") as f, ThreadPoolExecutor(6) as ex:
        for (a, b, kind), cs in ex.map(work, pairs):
            if cs is None:
                continue
            stats["pairs"] += 1
            stats["proposed"] += len(cs)
            stats["grounded"] += sum(c["quotes_found"] for c in cs)
            stats["confirmed"] += sum(c["confirmed"] for c in cs)
            for c in cs:
                c["human_label"] = None
            f.write(json.dumps({"kind": kind, "a": {k: a[k] for k in ("lang", "title", "url")},
                                "b": {k: b[k] for k in ("lang", "title", "url")},
                                "conflicts": cs}, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{kind}] {a['lang']}:{a['title']} <> {b['lang']}:{b['title']}: "
                  f"{len(cs)} proposed, {sum(c['confirmed'] for c in cs)} confirmed", flush=True)
    print(stats, "->", out)


if __name__ == "__main__":
    main()
