"""Cached yes/no questions about a pair of texts, asked through the Gemini Decisions API.

pair_check(kind, template, [(text_a, text_b)]) -> [P(yes)]. Answers are kept in the table pair_check,
keyed by a hash of the question and both texts, so the same pair is never asked twice.
"""
import hashlib

from .. import decisions
from . import store

MAX_PASSAGE = 1500
_ready = False


def _ensure(cur):
    global _ready
    if not _ready:
        cur.execute("CREATE TABLE IF NOT EXISTS pair_check (kind VARCHAR(16) NOT NULL, sha CHAR(40) NOT NULL, p_yes FLOAT NOT NULL, "
                    "created TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (kind, sha))")
        _ready = True


def pair_check_multi(specs, pairs):
    """specs: {kind: template}; pairs: [(text_a, text_b)] -> [{kind: p_yes}]. One request per pair asks every kind that is not cached yet."""
    conn = store.connect()
    conn.autocommit(True)
    try:
        cur = conn.cursor()
        _ensure(cur)
        keys = [{k: hashlib.sha1(f"{tpl}|{a[:MAX_PASSAGE]}|{b[:MAX_PASSAGE]}".encode()).hexdigest() for k, tpl in specs.items()} for a, b in pairs]
        have = {}
        allkeys = [s for ks in keys for s in ks.values()]
        for i in range(0, len(allkeys), 400):
            part = allkeys[i:i + 400]
            cur.execute(f"SELECT kind, sha, p_yes FROM pair_check WHERE sha IN ({','.join(['%s'] * len(part))})", part)
            have.update({(k, s): float(p) for k, s, p in cur.fetchall()})
        jobs, owners = [], []
        for i, ((a, b), ks) in enumerate(zip(pairs, keys)):
            missing = {k: specs[k] for k, s in ks.items() if (k, s) not in have}
            if missing:
                owners.append((i, list(missing)))
                jobs.append((a[:MAX_PASSAGE], {k: {"type": "noul", "instructions": tpl.format(b=b[:MAX_PASSAGE])} for k, tpl in missing.items()}))
        rows = []
        for (i, kinds), ans in zip(owners, decisions.ask_many(jobs)):
            for k in kinds:
                have[(k, keys[i][k])] = float(ans[k]["noul"])
                rows.append((k, keys[i][k], have[(k, keys[i][k])]))
        if rows:
            cur.executemany("INSERT IGNORE INTO pair_check (kind, sha, p_yes) VALUES (%s,%s,%s)", rows)
        return [{k: have[(k, s)] for k, s in ks.items()} for ks in keys]
    finally:
        conn.close()


def pair_check(kind, template, pairs):
    """pairs: [(text_a, text_b)] -> [p_yes] for one yes/no question, cached in pair_check."""
    return [r[kind] for r in pair_check_multi({kind: template}, pairs)]
