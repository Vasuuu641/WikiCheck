"""Load every version of the showcase articles from dataset M into article_state (db/002),
with Wikipedia's real edit times, following MariaDB's mariadb-system-versioning-wikipedia load.py.

    python eval/load_history.py                     # the showcase articles below
    python eval/load_history.py --titles "Lapin sota" "Tarton rauha"

Needs dataset M (release m-2026-10, 16 Parquet files) in data/m-2026-10/:
  https://github.com/MariaDB/mariadb-wikipedia-datasets/releases/tag/m-2026-10
Safe to run again: articles that already have rows in article_state are skipped.

How a version gets its time: MariaDB normally stamps each change with "now". With
SET system_versioning_insert_history = 1 it accepts row_start/row_end from us, so each version
starts when it was saved on Wikipedia and ends when the next version was saved. The newest
version is inserted without row_end, so it is the current row.
"""
import argparse
import glob
import sys
import time
from datetime import timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from api.search import connect  # noqa: E402

SHOWCASE = [
    "Suomen sisällissota", "Talvisota", "Jatkosota", "Urho Kekkonen",
    "Carl Gustaf Emil Mannerheim", "Lapuanliike", "Moskovan rauhansopimus (1940)",
    "Suomen itsenäisyysjulistus",
]
BATCH_BYTES = 8_000_000   # bytes of rows per INSERT; MariaDB's default max_allowed_packet is 16 MB
COLUMNS = ["page_id", "title", "rev_id", "timestamp", "user", "anonymous", "comment", "minor",
           "sha1", "bytes", "text"]
INSERT_COLS = "page_id, rev_id, title, editor, anonymous, edit_comment, minor, sha1, bytes, wikitext, row_start"


def read_versions(files: list[str], page_ids: set[int]) -> dict[int, list[dict]]:
    """All versions of the wanted pages. Files are sorted by page_id, so the row-group statistics
    let pyarrow skip almost everything."""
    found: dict[int, list[dict]] = {}
    for path in files:
        meta = pq.ParquetFile(path).metadata
        lo = meta.row_group(0).column(0).statistics.min
        hi = meta.row_group(meta.num_row_groups - 1).column(0).statistics.max
        if not any(lo <= p <= hi for p in page_ids):
            continue
        table = pq.read_table(path, columns=COLUMNS, filters=[("page_id", "in", sorted(page_ids))])
        # Stored as UTC; drop the zone label so Python gets naive UTC datetimes (pyarrow needs a
        # tz database to build aware ones, which Windows does not have).
        i = table.schema.get_field_index("timestamp")
        table = table.set_column(i, "timestamp", table.column(i).cast(pa.timestamp("ms")))
        for row in table.to_pylist():
            found.setdefault(row["page_id"], []).append(row)
        print(f"  {Path(path).name}: {table.num_rows} versions", flush=True)
    return found


def save(cur, con, versions: list[dict]) -> int:
    versions.sort(key=lambda v: (v["timestamp"], v["rev_id"]))
    times = [v["timestamp"] for v in versions]   # naive datetimes in UTC
    for i in range(1, len(times)):          # two saves in the same second: keep order, 1 µs apart
        if times[i] <= times[i - 1]:
            times[i] = times[i - 1] + timedelta(microseconds=1)

    def row(v, t):
        return (v["page_id"], v["rev_id"], v["title"], v["user"], bool(v["anonymous"]), v["comment"],
                bool(v["minor"]), v["sha1"], v["bytes"], v["text"] or "", t)

    con.begin()
    batch, size = [], 0
    history = [row(v, times[i]) + (times[i + 1],) for i, v in enumerate(versions[:-1])]
    for r in history + [None]:
        if batch and (r is None or size + len(r[9].encode()) > BATCH_BYTES):
            cur.executemany(f"INSERT INTO article_state ({INSERT_COLS}, row_end) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", batch)
            batch, size = [], 0
        if r is not None:
            batch.append(r)
            size += len(r[9].encode()) + 1000
    # The newest version: no row_end, so it stays the current row.
    cur.execute(f"INSERT INTO article_state ({INSERT_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                row(versions[-1], times[-1]))
    con.commit()
    return len(versions)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--titles", nargs="+", default=SHOWCASE, help="Finnish article titles")
    p.add_argument("--data", default=str(ROOT / "data" / "m-2026-10"))
    args = p.parse_args()

    files = sorted(glob.glob(f"{args.data}/versions-*.parquet"))
    if len(files) != 16:
        sys.exit(f"expected 16 versions-*.parquet files in {args.data}, found {len(files)}")

    con = connect()
    cur = con.cursor()
    cur.execute("SET time_zone = '+00:00'")                  # Wikipedia's times are UTC
    cur.execute("SET system_versioning_insert_history = 1")

    cur.execute("SELECT title, page_id FROM article WHERE lang = 'fi' AND title IN ({})"
                .format(",".join(["%s"] * len(args.titles))), args.titles)
    by_title = dict(cur.fetchall())
    for t in args.titles:
        if t not in by_title:
            print(f"warning: no Finnish article titled {t!r}")
    cur.execute("SELECT DISTINCT page_id FROM article_state FOR SYSTEM_TIME ALL")
    loaded = {p for (p,) in cur.fetchall()}
    todo = {pid for pid in by_title.values() if pid not in loaded}
    print(f"{len(by_title)} articles, {len(by_title) - len(todo)} already loaded, {len(todo)} to load")
    if not todo:
        return

    t0 = time.perf_counter()
    found = read_versions(files, todo)
    for title, pid in by_title.items():
        if pid not in todo:
            continue
        if pid not in found:
            print(f"warning: {title} (page {pid}) not in dataset M")
            continue
        t = time.perf_counter()
        n = save(cur, con, found[pid])
        mb = sum(len((v["text"] or "").encode()) for v in found[pid]) / 1e6
        print(f"  {title} (page {pid}): {n} versions, {mb:.0f} MB, {time.perf_counter() - t:.1f} s",
              flush=True)

    cur.execute("SELECT COUNT(DISTINCT page_id), COUNT(*) FROM article_state FOR SYSTEM_TIME ALL")
    print("article_state now: {} articles, {} versions ({:.0f} s)".format(
        *cur.fetchone(), time.perf_counter() - t0))
    con.close()


if __name__ == "__main__":
    main()
