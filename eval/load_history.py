"""Load every version of the showcase articles from dataset M into the history table
`article_version`, with Wikipedia's real edit times.

    pacome/.venv/bin/python eval/load_history.py                     # the showcase articles below
    pacome/.venv/bin/python eval/load_history.py --titles "Lapin sota" "Tarton rauha"

There is one history table for the whole app: `article_version`, owned by the site
(pacome/rag-demo/history.py). This script only calls its loader, `history.ensure_loaded(page_id)`,
so /api/ask as_of, /api/trace and the site's Opinion drift read the same rows. It therefore runs
with the site's Python (pacome/.venv: the `mariadb` driver and pyarrow) and needs dataset M in
pacome/data/m/ (pacome/scripts/load_m.py downloads it).

Safe to run again: articles already loaded are skipped. The site also loads an article by itself
the first time Opinion drift needs it.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pacome" / "rag-demo"))

SHOWCASE = [
    "Suomen sisällissota", "Talvisota", "Jatkosota", "Urho Kekkonen",
    "Carl Gustaf Emil Mannerheim", "Lapuanliike", "Moskovan rauhansopimus (1940)",
    "Suomen itsenäisyysjulistus",
]


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--titles", nargs="+", default=SHOWCASE, help="Finnish article titles")
    args = p.parse_args()

    try:
        import history  # pacome/rag-demo/history.py
        import rag
    except ImportError as e:
        sys.exit(f"{e}\nRun this with the site's Python: pacome/.venv/bin/python eval/load_history.py")

    conn = rag.connect()
    try:
        cur = conn.cursor()
        marks = ",".join("?" * len(args.titles))
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
