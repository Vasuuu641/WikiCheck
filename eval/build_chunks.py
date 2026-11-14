"""Fill chunk / chunk_vector for every Finnish article: paragraphs as api/ask.py splits them, each
embedded with bge-m3 as "<article title>: <paragraph>" (the plain paragraph is stored).

    python eval/build_chunks.py              # everything still to do
    python eval/build_chunks.py --limit 300  # only the next 300 articles (to time it)

Resumable: an article whose chunks all have its current rev_id is skipped, so stop it at any time
(Ctrl+C) and run it again. Each article is written in its own transaction. /api/ask writes the same
rows for the articles it answers from, with the same code, so the two can run at the same time.
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from api.ask import embed_paragraphs, split_paragraphs, write_chunks  # noqa: E402
from api.search import connect  # noqa: E402

GROUP_PARAGRAPHS = 256   # embed several articles' paragraphs together, then write those articles
REPORT_EVERY_S = 30


def to_do(cur) -> list[tuple[int, int, int]]:
    """(page_id, rev_id, length) of Finnish articles whose chunks are missing or stale."""
    cur.execute("SELECT page_id, MIN(rev_id), MAX(rev_id) FROM ask_chunk GROUP BY page_id")
    done = {pid: lo for pid, lo, hi in cur.fetchall() if lo == hi}
    cur.execute("SELECT page_id, rev_id, LENGTH(text) FROM article WHERE lang = 'fi' ORDER BY page_id")
    return [(pid, rev, n) for pid, rev, n in cur.fetchall() if done.get(pid) != rev]


def fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, help="only this many articles (to time a sample)")
    p.add_argument("--status", action="store_true", help="only show how far the build is, then exit")
    args = p.parse_args()

    con = connect()
    cur = con.cursor()
    if args.status:
        cur.execute("SELECT COUNT(*), SUM(LENGTH(text)) FROM article WHERE lang = 'fi'")
        total, total_chars = cur.fetchone()
        left = to_do(cur)
        cur.execute("SELECT COUNT(*) FROM ask_chunk")
        chunks = cur.fetchone()[0]
        cur.execute("SELECT IS_USED_LOCK('build_chunks')")
        running = cur.fetchone()[0] is not None
        left_chars = sum(n for _, _, n in left)
        print(f"{total - len(left)} of {total} Finnish articles chunked ({(total - len(left)) / total:.1%}), "
              f"{1 - left_chars / float(total_chars):.1%} of the text, {chunks} paragraphs; "
              f"{len(left)} articles left. A build holding the lock: {'yes' if running else 'no'}")
        return
    cur.execute("SELECT GET_LOCK('build_chunks', 0)")   # held until this connection closes
    if cur.fetchone()[0] != 1:
        sys.exit("another build_chunks.py is already running")
    cur.execute("SELECT COUNT(*) FROM article WHERE lang = 'fi'")
    total_articles = cur.fetchone()[0]
    todo = to_do(cur)
    print(f"{total_articles} Finnish articles, {total_articles - len(todo)} already chunked, "
          f"{len(todo)} to do", flush=True)
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        return
    total_chars = sum(n for _, _, n in todo)

    t0 = last = time.perf_counter()
    done_articles = done_paras = done_chars = 0
    embed_s = write_s = 0.0
    # Writing runs in a second thread (own connection) while the next group is embedded.
    writer, wcon, pending = ThreadPoolExecutor(1), connect(), None

    def write(group, vectors):
        nonlocal write_s
        t = time.perf_counter()
        it = iter(vectors)
        with wcon.cursor() as wcur:
            for pid, rev, _title, paras, _n in group:
                rows = [(para, next(it)) for para in paras]
                for attempt in range(3):   # a deadlock (1213) with /api/ask writing the same article
                    try:
                        wcon.begin()
                        write_chunks(wcur, pid, rev, rows)
                        wcon.commit()
                        break
                    except pymysql.err.OperationalError as e:
                        wcon.rollback()
                        if e.args[0] != 1213 or attempt == 2:
                            raise
                        time.sleep(1)
        write_s += time.perf_counter() - t

    def flush(group):
        nonlocal done_articles, done_paras, done_chars, embed_s, pending
        t = time.perf_counter()
        vectors = embed_paragraphs([(title, para) for _, _, title, paras, _ in group for para in paras])
        embed_s += time.perf_counter() - t
        if pending:
            pending.result()   # one write in flight at most; re-raises a failed write
        pending = writer.submit(write, group, vectors)
        done_articles += len(group)
        done_paras += sum(len(g[3]) for g in group)
        done_chars += sum(g[4] for g in group)

    group, in_group = [], 0
    for i in range(0, len(todo), 200):
        ids = [pid for pid, _, _ in todo[i:i + 200]]
        cur.execute("SELECT page_id, rev_id, title, text FROM article WHERE lang = 'fi' AND page_id IN ({})"
                    .format(",".join(["%s"] * len(ids))), ids)
        for pid, rev, title, text in sorted(cur.fetchall()):
            paras = split_paragraphs(text)
            group.append((pid, rev, title, paras, len(text)))
            in_group += len(paras)
            if in_group >= GROUP_PARAGRAPHS:
                flush(group)
                group, in_group = [], 0
                now = time.perf_counter()
                if now - last >= REPORT_EVERY_S:
                    last = now
                    el = now - t0
                    eta = el / done_chars * (total_chars - done_chars) if done_chars else 0
                    print(f"{done_articles}/{len(todo)} articles ({done_chars / total_chars:.1%} of the text), "
                          f"{done_paras} paragraphs, {done_paras / el:.0f}/s, elapsed {fmt(el)}, "
                          f"ETA {fmt(eta)}", flush=True)
    if group:
        flush(group)
    if pending:
        pending.result()
    writer.shutdown()
    wcon.close()

    el = time.perf_counter() - t0
    print(f"done: {done_articles} articles, {done_paras} paragraphs, {done_chars / 1e6:.1f} M chars "
          f"in {fmt(el)} (embedding {embed_s:.0f} s, writing {write_s:.0f} s)", flush=True)
    cur.execute("SELECT COUNT(DISTINCT page_id), COUNT(*) FROM ask_chunk")
    print("chunk table now: {} articles, {} chunks".format(*cur.fetchone()), flush=True)
    con.close()


if __name__ == "__main__":
    main()
