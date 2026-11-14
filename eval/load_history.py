"""Load every version of the showcase articles from dataset M into the history table
`article_version`, with Wikipedia's real edit times (make demo-data runs this).

    .venv/bin/python eval/load_history.py                     # the showcase articles below
    .venv/bin/python eval/load_history.py --titles "Lapin sota" "Tarton rauha"

It calls the drift engine's loader, api/drift/history.ensure_loaded(page_id), so Ask's as_of and
Opinion drift read the same rows. Needs dataset M in pacome/data/m/ (scripts/load_m.py
downloads it). Safe to run again: articles already loaded are skipped. Drift also loads an article
by itself the first time it needs it.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SHOWCASE = [
    "Suomen sisällissota", "Talvisota", "Jatkosota", "Urho Kekkonen",
    "Carl Gustaf Emil Mannerheim", "Lapuanliike", "Moskovan rauhansopimus (1940)",
    "Suomen itsenäisyysjulistus",
]


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--titles", nargs="+", default=SHOWCASE, help="Finnish article titles")
    args = p.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from api.drift import history, store

    conn = store.connect()
    try:
        cur = conn.cursor()
        marks = ",".join(["%s"] * len(args.titles))
        cur.execute(f"SELECT title, page_id FROM article WHERE lang = 'fi' AND title IN ({marks})", args.titles)
        page_ids = dict(cur.fetchall())
    finally:
        conn.close()

    t0 = time.time()
    for title in args.titles:
        pid = page_ids.get(title)
        if pid is None:
            print(f"  {title}: no Finnish article with this title, skipped")
            continue
        try:
            n = history.ensure_loaded(pid)
        except Exception as e:
            print(f"  {title}: {type(e).__name__}: {e}")
            continue
        print(f"  {title}: {'already loaded' if not n else f'{n} versions loaded'}")
    print(f"done ({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
