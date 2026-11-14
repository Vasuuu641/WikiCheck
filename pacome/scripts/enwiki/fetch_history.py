#!/usr/bin/env python3
"""Download English Wikipedia history chunks (.7z), keep only sampled articles, write Parquet.

  fetch_history.py --target 100000 [--workers 6]

Chunks are processed in a fixed random order. Every chunk is fully read (7z cannot be read partially) and
every article from sample_pages.parquet found in it is kept with ALL its revisions. Stops once at least
--target sampled articles are stored. Resumable: finished chunks leave data/enwiki/versions/<chunk>.parquet.
Raw .7z files are deleted after conversion.
"""
import argparse, fcntl, json, os, random, subprocess, sys, time
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
import pyarrow as pa, pyarrow.parquet as pq

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "enwiki")
SEVENZ = os.path.join(D, "..", "tools", "7z", "usr", "lib", "7zip", "7z")
BASE = "https://dumps.wikimedia.org"
OUT = os.path.join(D, "versions")
RAW = os.path.join(D, "raw")
COLS = ["page_id", "title", "rev_id", "parent_id", "timestamp", "user", "anonymous", "comment", "minor", "sha1", "bytes", "text"]


def local(el, name):
    for c in el:
        if c.tag.rsplit("}", 1)[-1] == name:
            return c
    return None


def txt(el, name):
    c = local(el, name)
    return None if c is None else c.text


DL_SLOTS = 2   # Wikimedia allows about 2 simultaneous connections per client


def download(src, url):
    """Download with at most DL_SLOTS concurrent downloads across all workers (file locks)."""
    while True:
        for i in range(DL_SLOTS):
            lk = open(os.path.join(RAW, f".dl{i}.lock"), "w")
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lk.close(); continue
            try:
                for attempt in range(8):
                    r = subprocess.run(["curl", "-sS", "--fail", "-C", "-", "-o", src, url])
                    if r.returncode == 0:
                        return
                    time.sleep(30 * (attempt + 1))   # 429 or network error: back off
                raise RuntimeError(f"download failed: {url}")
            finally:
                lk.close()
        time.sleep(2)


def process(job):
    name, url, wanted = job
    dst = os.path.join(OUT, name.replace(".7z", ".parquet"))
    if os.path.exists(dst):
        return name, -1, 0, 0
    src = os.path.join(RAW, name)
    t0 = time.time()
    download(src, BASE + url)
    rows = {c: [] for c in COLS}
    pages = 0
    p = subprocess.Popen([SEVENZ, "e", "-so", src], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    for _, el in ET.iterparse(p.stdout, events=("end",)):
        if el.tag.rsplit("}", 1)[-1] != "page":
            continue
        pid = int(txt(el, "id"))
        if pid in wanted and txt(el, "ns") == "0":
            pages += 1
            title = txt(el, "title")
            for r in el:
                if r.tag.rsplit("}", 1)[-1] != "revision":
                    continue
                contrib = local(r, "contributor")
                user = txt(contrib, "username") if contrib is not None else None
                ip = txt(contrib, "ip") if contrib is not None else None
                t = local(r, "text")
                par = txt(r, "parentid")
                rows["page_id"].append(pid); rows["title"].append(title)
                rows["rev_id"].append(int(txt(r, "id"))); rows["parent_id"].append(int(par) if par else None)
                rows["timestamp"].append(txt(r, "timestamp")); rows["user"].append(user or ip)
                rows["anonymous"].append(ip is not None); rows["comment"].append(txt(r, "comment"))
                rows["minor"].append(local(r, "minor") is not None); rows["sha1"].append(txt(r, "sha1"))
                rows["bytes"].append(int(t.get("bytes", 0)) if t is not None else None)
                rows["text"].append(t.text if t is not None else None)
        el.clear()
    if p.wait() != 0:
        raise RuntimeError(f"7z failed on {name}")
    tbl = pa.table(rows)
    pq.write_table(tbl, dst + ".tmp", compression="zstd", compression_level=9)
    os.replace(dst + ".tmp", dst)
    os.remove(src)
    return name, pages, len(rows["rev_id"]), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=100_000)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True); os.makedirs(RAW, exist_ok=True)
    wanted = set(pq.read_table(os.path.join(D, "sample_pages.parquet"), columns=["page_id"]).column(0).to_pylist())
    files = json.load(open(os.path.join(D, "history7z_files.json")))
    names = sorted(files)
    random.Random(20261001).shuffle(names)
    done_pages = 0
    for n in names:   # count pages already stored (resume)
        f = os.path.join(OUT, n.replace(".7z", ".parquet"))
        if os.path.exists(f):
            done_pages += len(set(pq.read_table(f, columns=["page_id"]).column(0).to_pylist()))
    print(f"already stored: {done_pages:,} articles", flush=True)
    todo = [n for n in names if not os.path.exists(os.path.join(OUT, n.replace(".7z", ".parquet")))]
    it = iter(todo)
    with ProcessPoolExecutor(a.workers) as ex:
        running = set()
        def submit():
            n = next(it, None)
            if n: running.add(ex.submit(process, (n, files[n]["url"], wanted)))
        for _ in range(a.workers): submit()
        while running and done_pages < a.target:
            time.sleep(1)
            for f in [f for f in running if f.done()]:
                running.discard(f)
                name, pages, revs, secs = f.result()
                done_pages += max(pages, 0)
                print(f"{name}: {pages} articles, {revs:,} revisions, {secs:.0f}s | total {done_pages:,}", flush=True)
                if done_pages < a.target: submit()
        for f in running:   # let in-flight chunks finish
            name, pages, revs, secs = f.result(); done_pages += max(pages, 0)
            print(f"{name}: {pages} articles, {revs:,} revisions, {secs:.0f}s | total {done_pages:,}", flush=True)
    print(f"done: {done_pages:,} articles")


if __name__ == "__main__":
    main()
