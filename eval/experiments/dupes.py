"""Experiment: near-duplicate / overlap detection inside one language, MariaDB vectors only.

For N random articles, take the nearest same-language neighbour (excluding itself) and also
measure raw text overlap (5-word shingle Jaccard) as an independent check on the vector signal.
  .venv/bin/python -m eval.experiments.dupes --n 300
"""
import argparse
import json
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
from api.search import connect  # noqa: E402

SQL = """
SELECT a.lang, a.page_id, a.title, a.url, a.text, v.distance FROM (
  SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding,
         (SELECT embedding FROM article_vector WHERE lang=%s AND page_id=%s)) AS distance
  FROM article_vector ORDER BY distance LIMIT 40) v
JOIN article a USING (lang, page_id) WHERE a.lang=%s ORDER BY v.distance LIMIT 4
"""


def shingles(t, k=5):
    w = re.findall(r"\w+", t.lower())
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def jaccard(a, b):
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / max(1, len(sa | sb))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--lang", default=None)
    args = ap.parse_args()
    out = []
    with connect() as con, con.cursor() as cur:
        cur.execute("SET mhnsw_ef_search = 80")
        q = "SELECT lang, page_id FROM article" + (" WHERE lang=%s" if args.lang else "") + \
            " ORDER BY RAND(11) LIMIT %s"
        cur.execute(q, ((args.lang,) if args.lang else ()) + (args.n,))
        seeds = cur.fetchall()
        for lang, pid in seeds:
            cur.execute(SQL, (lang, pid, lang))
            rows = cur.fetchall()
            me = next((r for r in rows if r[1] == pid), None)
            if not me:
                continue
            for r in rows:
                if r[1] == pid:
                    continue
                out.append({"lang": lang, "a": me[2], "b": r[2], "a_url": me[3], "b_url": r[3],
                            "distance": round(float(r[5]), 4),
                            "jaccard": round(jaccard(me[4], r[4]), 3),
                            "len_a": len(me[4]), "len_b": len(r[4])})
                break  # nearest neighbour only
    p = Path(__file__).resolve().parents[1] / "results" / "dupes.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"{len(out)} nearest-neighbour pairs")
    for lo, hi in [(0, .08), (.08, .12), (.12, .16), (.16, .2), (.2, .3), (.3, 1)]:
        b = [o for o in out if lo <= o["distance"] < hi]
        print(f"dist {lo:.2f}-{hi:.2f}: {len(b):3d} pairs, median jaccard "
              f"{sorted(x['jaccard'] for x in b)[len(b) // 2] if b else '-'}")
    print("\nclosest pairs:")
    for o in sorted(out, key=lambda o: o["distance"])[:25]:
        print(f"  d={o['distance']:.3f} J={o['jaccard']:.2f} [{o['lang']}] {o['a']}  <>  {o['b']}")


if __name__ == "__main__":
    main()
