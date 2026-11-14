#!/usr/bin/env python3
"""Checks for the Tier B tools.   .venv/bin/python scripts/eval_tier_b.py b2|b3|b1-sample|b4-sample [--n N]

  b2         remove the ref from N sourced paragraphs (in memory), ask the tool, report how often the same source comes back
  b3         hide N existing links (in memory), report how often the suggester proposes them back
  b1-sample  dump a random sample of B1 results (for hand labelling)
  b4-sample  run B4 on random chunks and dump the flagged pairs (for hand labelling)
Nothing is written to the existing tables; b2 and b4 add rows to the pair_check cache.
"""
import argparse
import json
import os
import random
import re
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rag-demo"))
import consistency  # noqa: E402
import gaps  # noqa: E402
import links  # noqa: E402
import rag  # noqa: E402
import refs  # noqa: E402
import tb  # noqa: E402


POOL_OVERRIDE = int(os.environ.get("POOL", 0))


def norm(s):
    return re.sub(r"[^a-zåäö0-9]+", " ", s.lower()).strip()


def loose(s):
    """Same work, ignoring page numbers: the first 30 letters and digits of the normalised text, pages and years kept out."""
    return re.sub(r"\b(s|sivu|sivut|p|pp)\b.*$", "", norm(s)).strip()[:40]


def b2(n):
    conn = rag.connect()
    cur = conn.cursor()
    cur.execute("SELECT p.page_id, p.para_no, r.ref_text FROM para p JOIN para_ref r USING (page_id, para_no) WHERE p.has_ref")
    by_para, by_ref = {}, {}
    for pid, no, txt in cur.fetchall():
        by_para.setdefault((pid, no), []).append(txt)
        by_ref.setdefault(norm(txt), set()).add(pid)
    keys = sorted(by_para)
    random.Random(7).shuffle(keys)
    rows = []
    stats = dict(n=0, possible=0, retr_exact=0, retr_loose=0, conf_exact=0, conf_loose=0, proposed=0)
    for pid, no in keys[:n]:
        cur.execute("SELECT text FROM para WHERE page_id = ? AND para_no = ?", (pid, no))
        text = cur.fetchone()[0]
        truth = by_para[(pid, no)]
        cands, info = refs.candidates(cur, pid, no)         # the paragraph's own page is excluded, so its own ref cannot leak in
        refs.confirmed(text, cands)
        t_exact = {norm(t) for t in truth}
        t_loose = {loose(t) for t in truth}
        possible = any(by_ref[norm(t)] - {pid} for t in truth)
        def hit(cs, f, tt):
            return any(f(r) in tt for c in cs for r in c["refs"])
        conf = [c for c in cands if c["p_same"] >= refs.CONFIRM]
        stats["n"] += 1
        stats["possible"] += possible
        stats["retr_exact"] += hit(cands, norm, t_exact)
        stats["retr_loose"] += hit(cands, loose, t_loose)
        stats["conf_exact"] += hit(conf, norm, t_exact)
        stats["conf_loose"] += hit(conf, loose, t_loose)
        stats["proposed"] += bool(conf)
        rows.append(dict(page_id=pid, para_no=no, text=text[:300], truth=truth, possible=possible,
                         candidates=[dict(title=c["title"], d=c["distance"], p=c["p_same"], refs=c["refs"]) for c in cands]))
    print(json.dumps(stats))
    json.dump(rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tier_b_b2.json"), "w"), ensure_ascii=False, indent=1)


def b3(n):
    conn = rag.connect()
    cur = conn.cursor()
    cur.execute("SELECT l.from_page_id, l.to_title, a.page_id FROM article_link l JOIN article a ON a.lang = 'fi' AND a.title = l.to_title "
                "WHERE l.from_page_id IN (SELECT DISTINCT page_id FROM para) AND a.page_id IN (SELECT DISTINCT page_id FROM para) "
                "AND a.page_id <> l.from_page_id")
    allow = cur.fetchall()
    cur.execute("SELECT from_page_id, to_title FROM article_link")
    out = {}
    for f, t in cur.fetchall():
        out.setdefault(f, set()).add(t)
    cur.execute("SELECT page_id, title FROM article WHERE lang = 'fi'")
    title = dict(cur.fetchall())
    # a pair is only eligible for the tool when no link exists in either direction, so use links that have no reverse link
    pool = [(f, t, p) for f, t, p in allow if title[f] not in out.get(p, set())]
    print(f"{len(allow)} links between articles of the set, {len(pool)} without a reverse link")
    random.Random(11).shuffle(pool)
    ranks, missing = [], 0
    for f, t, p in pool[:n]:
        _, items, _ = links.suggest_for(cur, f, hidden={t}, show=1000, pool=POOL_OVERRIDE or links.POOL)
        ids = [i["page_id"] for i in items]
        if p in ids:
            ranks.append(ids.index(p) + 1)
        else:
            missing += 1
    k = len(ranks) + missing
    for cut in (1, 3, 5, 10, 25):
        print(f"suggested back in the top {cut}: {sum(r <= cut for r in ranks)}/{k} = {sum(r <= cut for r in ranks) / k:.0%}")
    print(f"found anywhere in the pool: {len(ranks)}/{k}; median rank {statistics.median(ranks) if ranks else None}; "
          f"avg suggestions per article: shown list is capped at {links.SHOW}")


def b1_sample(n):
    conn = rag.connect()
    cur = conn.cursor()
    cur.execute("SELECT x.wikidata_id, x.lang, x.page_id, y.lang, y.page_id FROM article x JOIN article y ON x.wikidata_id = y.wikidata_id AND x.lang <> y.lang "
                "WHERE x.lang IN ('sv', 'en') AND y.lang = 'fi' AND x.page_id IN (SELECT page_id FROM chunk WHERE lang = x.lang) "
                "AND y.page_id IN (SELECT page_id FROM chunk WHERE lang = 'fi')")
    pairs = cur.fetchall()
    random.Random(5).shuffle(pairs)
    rows = []
    for w, sl, sp, dl, dp in pairs[:n]:
        items, _ = gaps.gaps(cur, sl, sp, dl, dp)
        for it in items:
            rows.append(dict(wid=w, src=sl, **it))
    json.dump(rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tier_b_b1.json"), "w"), ensure_ascii=False, indent=1)
    print(len(rows), "chunks from", len(pairs[:n]), "topic pairs")


def b4_sample(n, lang):
    conn = rag.connect()
    cur = conn.cursor()
    cur.execute("SELECT lang, page_id, chunk_no FROM chunk WHERE lang = ? AND CHAR_LENGTH(text) >= 200 "
                "AND page_id IN (SELECT DISTINCT page_id FROM para) ORDER BY RAND(3) LIMIT ?", (lang, n))
    rows = []
    for l, p, c in cur.fetchall():
        src, items, _ = consistency.check_chunk(cur, l, p, c)
        for it in items:
            rows.append(dict(lang=l, src=src, **it))
    json.dump(rows, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tier_b_b4.json"), "w"), ensure_ascii=False, indent=1)
    flagged = [r for r in rows if r["p_incompatible"] >= consistency.FLAG]
    print(f"{len(rows)} pairs checked, {len(flagged)} flagged")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["b2", "b3", "b1-sample", "b4-sample"])
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--lang", default="fi")
    a = ap.parse_args()
    {"b2": lambda: b2(a.n), "b3": lambda: b3(a.n), "b1-sample": lambda: b1_sample(a.n), "b4-sample": lambda: b4_sample(a.n, a.lang)}[a.what]()
