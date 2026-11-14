#!/usr/bin/env python3
"""Download dataset M (every version of the Finnish History-of-Finland articles) and index it.

  scripts/load_m.py            download data/m/versions-01..16.parquet, verify SHA256SUMS, write data/m/index.parquet

index.parquet: page_id, title, file, versions  (one row per article and file it appears in).
Resumable: files that already match their checksum are skipped.
"""
import hashlib, os, subprocess, sys
import pyarrow as pa, pyarrow.parquet as pq

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "m")
BASE = "https://github.com/MariaDB/mariadb-wikipedia-datasets/releases/download/m-2026-10/"
FILES = [f"versions-{i:02d}.parquet" for i in range(1, 17)]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def fetch(name):
    dst = os.path.join(D, name)
    r = subprocess.run(["curl", "-sSL", "--fail", "--retry", "5", "-C", "-", "-o", dst, BASE + name])
    if r.returncode != 0:
        sys.exit(f"download failed: {name}")


def main():
    os.makedirs(D, exist_ok=True)
    fetch("SHA256SUMS")
    sums = {}
    for line in open(os.path.join(D, "SHA256SUMS")):
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*").split("/")[-1]] = parts[0]
    for name in FILES:
        dst = os.path.join(D, name)
        want = sums.get(name)
        if os.path.exists(dst) and want and sha256(dst) == want:
            print(f"{name}: ok (already here)", flush=True)
            continue
        if os.path.exists(dst) and want:
            os.remove(dst)          # partial or corrupt: start again
        fetch(name)
        got = sha256(dst)
        if want and got != want:
            sys.exit(f"{name}: checksum mismatch")
        print(f"{name}: downloaded{'' if want else ' (no checksum listed)'}", flush=True)

    rows = {"page_id": [], "title": [], "file": [], "versions": []}
    for name in FILES:
        t = pq.read_table(os.path.join(D, name), columns=["page_id", "title"])
        counts, titles = {}, {}
        for pid, title in zip(t.column(0).to_pylist(), t.column(1).to_pylist()):
            counts[pid] = counts.get(pid, 0) + 1
            titles[pid] = title
        for pid, n in counts.items():
            rows["page_id"].append(pid); rows["title"].append(titles[pid]); rows["file"].append(name); rows["versions"].append(n)
    pq.write_table(pa.table(rows), os.path.join(D, "index.parquet"))
    print(f"index: {len(set(rows['page_id'])):,} articles, {sum(rows['versions']):,} versions")


if __name__ == "__main__":
    main()
