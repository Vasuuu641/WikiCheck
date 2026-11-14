#!/usr/bin/env python3
"""Sample N random English Wikipedia articles from the page table dump.

Keeps namespace 0, non-redirect pages, shuffles with a fixed seed, takes the first N.
Output: data/enwiki/sample_pages.parquet  (page_id, rank, title, len), rank = position in the shuffle,
so any prefix (rank < 100000, ...) is itself a uniform random sample.
"""
import gzip, os, random, re, sys
import pyarrow as pa, pyarrow.parquet as pq

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "enwiki")
SRC = os.path.join(D, "raw", "enwiki-20261001-page.sql.gz")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
SEED = 20261001

# (page_id, namespace, 'title', is_redirect, is_new, random, touched, links_updated, latest, len,
ROW = re.compile(rb"\((\d+),(\d+),'((?:[^'\\]|\\.)*)',(\d),\d,[\d.eE+-]+,'?\d*'?,'?\w*'?,\d+,(\d+),")
ids, titles, lens = [], [], []
total = redir = 0


def scan(body):
    global total, redir
    for m in ROW.finditer(body):
        pid, ns, title, isred, ln = m.groups()
        if ns != b"0":
            continue
        total += 1
        if isred == b"1":
            redir += 1
            continue
        ids.append(int(pid)); titles.append(title.decode("utf-8", "replace")); lens.append(int(ln))


with gzip.open(SRC, "rb") as f:
    tail = b""
    while chunk := f.read(1 << 24):
        buf = tail + chunk
        cut = buf.rfind(b"),(") + 1   # keep a possibly incomplete last row for the next round
        scan(buf[:cut]); tail = buf[cut:]
    scan(tail)
print(f"namespace-0 pages {total:,}, redirects {redir:,}, articles {len(ids):,}")
order = list(range(len(ids)))
random.Random(SEED).shuffle(order)
order = order[:N]
tbl = pa.table({"page_id": [ids[i] for i in order], "rank": list(range(len(order))),
                "title": [titles[i] for i in order], "len": [lens[i] for i in order]})
pq.write_table(tbl, os.path.join(D, "sample_pages.parquet"), compression="zstd")
print(f"wrote {len(order):,} sampled articles")
