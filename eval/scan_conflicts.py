"""Offline conflict scan for editors: pairs of paragraphs from DIFFERENT Finnish articles that
state an incompatible fact (a date, a year, a number, a name or an order of events). Confirmed
pairs go to possible_conflict (db/005_conflict.sql) and GET /api/conflicts serves them.

    python eval/scan_conflicts.py --category "Porvoon keskiaika" --limit 200
    python eval/scan_conflicts.py --category A --category B --neighbours 5 --max-distance 0.3
    python eval/scan_conflicts.py --category A --coverage      # only: are these articles chunked?

  1. Seeds: chunks of Finnish articles in the categories and their subcategories (the recursive
     category query from MariaDB's mariadb-vector-wikipedia-rag README) that contain a digit.
     Several categories are taken round-robin, so --limit samples all of them.
  2. Candidates: for each seed its nearest chunks of OTHER articles, by the seed's stored vector
     (vector index in a subquery on chunk_vector, then the joins; no Ollama). Kept when the
     distance is under --max-distance and both texts contain a number or a date. (a, b) and
     (b, a) are one pair; pairs already in possible_conflict or already checked are skipped.
     --limit stops after that many new candidate pairs.
  3. Check: the review LLM (api/llm.py role "review") gets 10 pairs per call and returns, for a
     conflict, the exact phrase from each side (checked: it must occur in that text), what it is
     about, and kind "contradiction" or "sources may differ".
  4. Save: confirmed pairs are inserted (INSERT IGNORE on the pair key) after every call.

Resumable: every checked pair (conflict or not) is appended to data/conflict_scan_checked.jsonl,
keyed by both texts and the model, so a rerun skips it; Ctrl+C loses at most the calls in flight.
Needs the chunk build (eval/build_chunks.py) to be finished for complete results.
"""
import argparse
import hashlib
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from api.llm import chat, settings  # noqa: E402
from api.search import connect  # noqa: E402

CHECKED_FILE = ROOT / "data" / "conflict_scan_checked.jsonl"
BATCH = 10               # pairs per LLM call
MAX_TOKENS = 4000
REPORT_EVERY_S = 30
KINDS = ("contradiction", "sources may differ")

# A number, or a date written with a Finnish month name ("toukokuussa", "4. kesäkuuta").
HAS_NUMBER = re.compile(r"\d|(tammi|helmi|maalis|huhti|touko|kesä|heinä|elo|syys|loka|marras|joulu)kuu",
                        re.I)

CATS_CTE = """WITH RECURSIVE cats AS (
  SELECT CAST(%s AS VARCHAR(255)) AS name
  UNION
  SELECT l.child FROM category_link l JOIN cats ON l.parent = cats.name)"""

SEEDS_SQL = CATS_CTE + """
SELECT c.page_id, c.chunk_no, c.text, a.title, VEC_ToText(v.embedding)
FROM chunk c
JOIN chunk_vector v USING (page_id, chunk_no)
JOIN article a ON a.lang = 'fi' AND a.page_id = c.page_id
WHERE EXISTS (SELECT 1 FROM article_category ac JOIN cats ON ac.category = cats.name
              WHERE ac.lang = 'fi' AND ac.page_id = c.page_id)
  AND c.text REGEXP '[0-9]'
ORDER BY c.page_id, c.chunk_no"""

COVERAGE_SQL = CATS_CTE + """
SELECT a.page_id, a.title, a.rev_id, MIN(c.rev_id), MAX(c.rev_id), COUNT(c.chunk_no)
FROM article a
LEFT JOIN chunk c ON c.page_id = a.page_id
WHERE a.lang = 'fi'
  AND EXISTS (SELECT 1 FROM article_category ac JOIN cats ON ac.category = cats.name
              WHERE ac.lang = 'fi' AND ac.page_id = a.page_id)
GROUP BY a.page_id, a.title, a.rev_id
ORDER BY a.title"""

# Same pattern as api/search.py: ORDER BY distance LIMIT in the subquery so the vector index is
# used, then the joins. Over-fetched, because some of the nearest chunks are the seed's own article.
NEIGHBOURS_SQL = """
SELECT v.page_id, v.chunk_no, v.distance, c.text, a.title
FROM (SELECT page_id, chunk_no, VEC_DISTANCE_COSINE(embedding, VEC_FromText(%s)) AS distance
      FROM chunk_vector
      ORDER BY distance
      LIMIT %s) AS v
JOIN chunk c USING (page_id, chunk_no)
JOIN article a ON a.lang = 'fi' AND a.page_id = v.page_id
ORDER BY v.distance"""

INSERT_SQL = """
INSERT IGNORE INTO possible_conflict
  (about, kind, page_id_a, chunk_no_a, phrase_a, page_id_b, chunk_no_b, phrase_b,
   distance, seed_category, model)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""

SYSTEM_PROMPT = """You check Finnish Wikipedia for factual conflicts between articles, as an \
experienced Wikipedia editor would.

You get numbered pairs. Each pair is two paragraphs, A and B, from two DIFFERENT articles. For each \
pair decide: do A and B state an incompatible fact about the SAME thing? That is a different date, \
year, number, name, or order of events for the same person, event, office or object, so that both \
cannot be true.

NOT conflicts (answer "conflict": false):
- different scopes: one town vs the whole country, one year vs a whole period, a part vs the whole
- a rounded or approximate number vs an exact one ("noin 300" vs "312", "1500-luvulla" vs "1554")
- different events, people, offices or terms that only look alike (two bishops of the same name, \
a first and a second appointment, election vs taking office, start vs end of a term)
- different points in time (a figure "by 2020" vs "by 2021")
- one text simply saying more than the other, or not mentioning the fact at all
When in doubt, it is not a conflict. Most pairs are not conflicts.

For a conflict give:
- "a" and "b": the shortest phrase that states the fact, copied EXACTLY, character for character, \
from A and from B (include the number or date)
- "about": what they disagree on, one line in Finnish (e.g. "Maunu Tavastin piispaksi \
vihkimisen vuosi")
- "kind": "contradiction", or "sources may differ" when the fact is old, estimated or uncertain \
and historians plausibly disagree (medieval dates, population or casualty estimates)

Reply with JSON only, no other text, one object per pair:
{"pairs": [{"n": 1, "conflict": false},
           {"n": 2, "conflict": true, "about": "...", "kind": "contradiction", "a": "...", "b": "..."}]}"""


def fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def pair_hash(text_a: str, text_b: str, model: str) -> str:
    return hashlib.sha1(f"{model}\x00{text_a}\x00{text_b}".encode("utf-8")).hexdigest()


def load_checked() -> set[str]:
    if not CHECKED_FILE.exists():
        return set()
    out = set()
    with open(CHECKED_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                out.add(json.loads(line)["h"])
            except (ValueError, KeyError):
                continue   # a line cut short by Ctrl+C
    return out


def coverage(cur, categories: list[str]) -> bool:
    """Print, per category, how many of its articles are chunked at their current revision.
    True when all are."""
    all_ok = True
    for cat in categories:
        cur.execute(COVERAGE_SQL, (cat,))
        rows = cur.fetchall()
        missing = [(pid, t) for pid, t, rev, lo, hi, n in rows if n == 0]
        stale = [(pid, t) for pid, t, rev, lo, hi, n in rows if n and not (lo == hi == rev)]
        ok = len(rows) - len(missing) - len(stale)
        print(f"  {cat}: {len(rows)} articles, {ok} chunked, {len(missing)} without chunks, "
              f"{len(stale)} with chunks of an older revision")
        for label, items in (("no chunks", missing), ("old revision", stale)):
            for pid, title in items:
                print(f"      {label}: {title} (page_id {pid})")
        all_ok = all_ok and not missing and not stale and rows
    return bool(all_ok)


def load_seeds(cur, categories: list[str]) -> dict[str, list[dict]]:
    """{category: [seed]}; a chunk in several categories is a seed of the first one only."""
    seen, by_cat = set(), {}
    for cat in categories:
        cur.execute(SEEDS_SQL, (cat,))
        seeds = []
        for pid, no, text, title, vec in cur.fetchall():
            if (pid, no) in seen:
                continue
            seen.add((pid, no))
            seeds.append({"page_id": pid, "chunk_no": no, "text": text, "title": title, "vec": vec,
                          "category": cat})
        by_cat[cat] = seeds
    return by_cat


def round_robin(by_cat: dict[str, list]):
    lists = [list(v) for v in by_cat.values()]
    for i in range(max((len(v) for v in lists), default=0)):
        for v in lists:
            if i < len(v):
                yield v[i]


def ordered(seed: dict, nb: dict, distance: float) -> dict:
    """The pair with side a = the lower page_id, as possible_conflict stores it."""
    sa = {k: seed[k] for k in ("page_id", "chunk_no", "text", "title")}
    sb = {k: nb[k] for k in ("page_id", "chunk_no", "text", "title")}
    a, b = (sa, sb) if sa["page_id"] < sb["page_id"] else (sb, sa)
    return {"a": a, "b": b, "distance": float(distance), "category": seed["category"]}


def find_candidates(cur, by_cat, args, saved: set, checked: set, model: str, stats: dict) -> list[dict]:
    total_seeds = sum(len(v) for v in by_cat.values())
    cur.execute("SET mhnsw_ef_search = %s", (max(80, args.neighbours * 4 + 20),))
    fetch = args.neighbours * 4 + 20
    out, keys = [], set()
    t0 = last = time.perf_counter()
    dists_kept = []
    for i, seed in enumerate(round_robin(by_cat), 1):
        if args.limit and len(out) >= args.limit:
            break
        st = stats[seed["category"]]
        st["seeds_used"] += 1
        cur.execute(NEIGHBOURS_SQL, (seed["vec"], fetch))
        cols = [c[0] for c in cur.description]
        others = [dict(zip(cols, r)) for r in cur.fetchall() if r[0] != seed["page_id"]][:args.neighbours]
        for nb in others:
            if nb["distance"] > args.max_distance:
                st["too_far"] += 1
                continue
            if not HAS_NUMBER.search(nb["text"]):
                st["no_number"] += 1
                continue
            p = ordered(seed, nb, nb["distance"])
            key = (p["a"]["page_id"], p["a"]["chunk_no"], p["b"]["page_id"], p["b"]["chunk_no"])
            if key in keys:
                st["duplicate"] += 1
                continue
            keys.add(key)
            if key in saved:
                st["already_saved"] += 1
                continue
            p["h"] = pair_hash(p["a"]["text"], p["b"]["text"], model)
            if p["h"] in checked:
                st["already_checked"] += 1
                continue
            out.append(p)
            st["candidates"] += 1
            dists_kept.append(p["distance"])
            if args.limit and len(out) >= args.limit:
                break
        now = time.perf_counter()
        if now - last >= REPORT_EVERY_S:
            last = now
            print(f"  candidates: {i}/{total_seeds} seeds, {len(out)} pairs, elapsed {fmt(now - t0)}",
                  flush=True)
    el = time.perf_counter() - t0
    print(f"candidates: {len(out)} new pairs from {sum(s['seeds_used'] for s in stats.values())} seeds "
          f"in {fmt(el)}", flush=True)
    if dists_kept:
        q = statistics.quantiles(dists_kept, n=4) if len(dists_kept) > 1 else [dists_kept[0]] * 3
        print(f"  distance of kept pairs: min {min(dists_kept):.3f}, quartiles "
              f"{q[0]:.3f} / {q[1]:.3f} / {q[2]:.3f}, max {max(dists_kept):.3f}", flush=True)
    return out


def _clip(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else text[:limit] + " ..."


def check_batch(pairs: list[dict]) -> tuple[list[dict], list[str]]:
    """One LLM call. Returns (confirmed conflicts, warnings)."""
    blocks = []
    for n, p in enumerate(pairs, 1):
        blocks.append(f"[{n}]\nA (article: {p['a']['title']}):\n{_clip(p['a']['text'])}\n"
                      f"B (article: {p['b']['title']}):\n{_clip(p['b']['text'])}")
    reply = chat([{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": "Pairs:\n\n" + "\n\n".join(blocks)}],
                 max_tokens=MAX_TOKENS, role="review")
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        raise ValueError(f"review model did not return JSON: {reply[:200]!r}")
    data = json.loads(m.group(0))
    found, warnings = [], []
    for item in data.get("pairs") or []:
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(pairs) or not item.get("conflict"):
            continue
        p = pairs[n - 1]
        fa, fb = str(item.get("a") or "").strip(), str(item.get("b") or "").strip()
        if not fa or not fb or fa not in p["a"]["text"] or fb not in p["b"]["text"]:
            warnings.append(f"dropped (phrase not in text): {p['a']['title']} / {p['b']['title']}: "
                            f"{fa!r} / {fb!r}")
            continue
        kind = str(item.get("kind") or "").strip().lower()
        found.append({**p, "phrase_a": fa, "phrase_b": fb,
                      "about": (str(item.get("about") or "").strip() or None),
                      "kind": kind if kind in KINDS else "contradiction"})
    return found, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--category", action="append", required=True,
                    help="category name without the 'Luokka:' prefix; may be given several times")
    ap.add_argument("--limit", type=int, help="stop after this many new candidate pairs")
    ap.add_argument("--neighbours", type=int, default=5, help="nearest chunks of other articles per seed")
    ap.add_argument("--max-distance", type=float, default=0.30, help="cosine distance threshold")
    ap.add_argument("--workers", type=int, default=1, help="LLM calls in parallel")
    ap.add_argument("--coverage", action="store_true", help="only check that the articles are chunked")
    args = ap.parse_args()

    con = connect()
    cur = con.cursor()
    print("chunk coverage:", flush=True)
    covered = coverage(cur, args.category)
    if args.coverage:
        return
    if not covered:
        print("warning: some articles are not chunked (or chunked at an old revision); "
              "their paragraphs are missing from the scan", flush=True)
    cur.execute("SELECT IS_USED_LOCK('build_chunks')")
    if cur.fetchone()[0] is not None:
        print("warning: eval/build_chunks.py is still running; neighbours from unbuilt articles "
              "are missing", flush=True)
    cur.execute("SHOW TABLES LIKE 'possible_conflict'")
    if not cur.fetchone():
        sys.exit("table possible_conflict is missing: apply db/005_conflict.sql first")

    model = settings("review")["model"]
    print(f"review model: {model}", flush=True)
    cur.execute("SELECT page_id_a, chunk_no_a, page_id_b, chunk_no_b FROM possible_conflict")
    saved = set(cur.fetchall())
    checked = load_checked()
    print(f"{len(saved)} conflicts already saved, {len(checked)} pairs already checked", flush=True)

    t_start = time.perf_counter()
    by_cat = load_seeds(cur, args.category)
    stats = {c: {"seeds": len(v), "seeds_used": 0, "candidates": 0, "conflicts": 0,
                 "contradiction": 0, "sources may differ": 0, "too_far": 0, "no_number": 0,
                 "duplicate": 0, "already_saved": 0, "already_checked": 0}
             for c, v in by_cat.items()}
    print(f"seeds: {sum(len(v) for v in by_cat.values())} chunks with a number "
          f"({', '.join(f'{c}: {len(v)}' for c, v in by_cat.items())})", flush=True)

    pairs = find_candidates(cur, by_cat, args, saved, checked, model, stats)

    t_check = time.perf_counter()
    batches = [pairs[i:i + BATCH] for i in range(0, len(pairs), BATCH)]
    done = found_total = dropped = failed = 0
    CHECKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKED_FILE, "a", encoding="utf-8") as log, ThreadPoolExecutor(max(1, args.workers)) as pool:
        futures = [(b, pool.submit(check_batch, b)) for b in batches]
        for batch, fut in futures:
            try:
                found, warnings = fut.result()
            except Exception as e:   # one bad reply must not stop the scan; the batch is retried next run
                failed += len(batch)
                print(f"  batch failed, not marked checked: {type(e).__name__}: {e}", flush=True)
                continue
            for w in warnings:
                print("  " + w, flush=True)
            dropped += len(warnings)
            for c in found:
                cur.execute(INSERT_SQL, (c["about"], c["kind"], c["a"]["page_id"], c["a"]["chunk_no"],
                                         c["phrase_a"], c["b"]["page_id"], c["b"]["chunk_no"],
                                         c["phrase_b"], c["distance"], c["category"], model))
                st = stats[c["category"]]
                st["conflicts"] += 1
                st[c["kind"]] += 1
                print(f"  CONFLICT ({c['kind']}, d={c['distance']:.3f}) {c['about']}\n"
                      f"      A {c['a']['title']}: {c['phrase_a']!r}\n"
                      f"      B {c['b']['title']}: {c['phrase_b']!r}", flush=True)
            for p in batch:
                log.write(json.dumps({"h": p["h"], "a": [p["a"]["page_id"], p["a"]["chunk_no"]],
                                      "b": [p["b"]["page_id"], p["b"]["chunk_no"]]}) + "\n")
            log.flush()
            done += len(batch)
            found_total += len(found)
            el = time.perf_counter() - t_check
            eta = el / done * (len(pairs) - done - failed) if done else 0
            print(f"checked {done + failed}/{len(pairs)} pairs, {found_total} conflicts, "
                  f"{done / el:.2f} pairs/s, elapsed {fmt(el)}, ETA {fmt(eta)}", flush=True)

    check_s = time.perf_counter() - t_check
    print("\nper category:")
    for c, st in stats.items():
        print(f"  {c}: {st['seeds']} seeds ({st['seeds_used']} used), {st['candidates']} candidate pairs, "
              f"{st['conflicts']} conflicts ({st['contradiction']} contradiction, "
              f"{st['sources may differ']} sources may differ); skipped: {st['too_far']} too far, "
              f"{st['no_number']} no number, {st['duplicate']} duplicate, "
              f"{st['already_saved']} already saved, {st['already_checked']} already checked")
    print(f"\ntotal: {sum(len(v) for v in by_cat.values())} seeds "
          f"({sum(s['seeds_used'] for s in stats.values())} used), {len(pairs)} candidate pairs, "
          f"{found_total} conflicts saved, {dropped} dropped (phrase not in text), "
          f"{failed} pairs in failed calls")
    print(f"time: checking {fmt(check_s)} ({len(batches)} LLM calls), total {fmt(time.perf_counter() - t_start)}")
    con.close()


if __name__ == "__main__":
    main()
