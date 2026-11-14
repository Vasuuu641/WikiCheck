"""Gemini Decisions API client (model gemini-decision-1): many questions about one text in one request.

  score(text, claims)   how a text treats each claim -> {claim_id: {"stance": -1..1, "presence": 0..1}}, cached in MariaDB
  ask(text, questions)  raw call, returns the API's "answers" dict
  score_many(jobs)      [(text, claims), ...] in a thread pool, results in order

  python decisions.py probe      one real request, prints the raw response
  python decisions.py validate   agreement on a small hand-labelled Finnish set

Request shape confirmed against the live API on 2026-11-14:
  POST /v1beta/decisions  {"model", "input", "questions": {key: {"type", "instructions", "criteria"}}}
  -> {"answers": {key: {"type", "confidence", "noul" | "score" + "probabilities" {"0".."n"} + "legend"}}, "usage": {"total_input_tokens"}}
"""
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import rag

URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
MODEL = "gemini-decision-1"
TOKENS_PER_MIN = 800_000        # tier 1 allows 1,000,000
POOL = 32
MAX_QUESTIONS = 1000

LEVELS = [
    "The text denies or contradicts the claim.",
    "The text mentions the claim but presents it as disputed, as a minority view, or as someone's accusation that it does not endorse.",
    "The text does not address the claim at all.",
    "The text states the claim with attribution or hedging (according to some, probably, it has been argued).",
    "The text states the claim as plain fact in its own voice.",
]
INSTRUCTIONS = ("The text is one version of a Finnish Wikipedia article. Judge only what this text says, not what is true. "
                "How does the text treat the following claim? Claim: {claim}")
_RUBRIC_SHA = hashlib.sha1((INSTRUCTIONS + "|".join(LEVELS)).encode()).hexdigest()

_stats = {"requests": 0, "tokens": 0, "cache_hits": 0}
_lock = threading.Lock()
_bucket = {"tokens": float(TOKENS_PER_MIN), "at": time.monotonic()}
_pool = ThreadPoolExecutor(POOL)
_table_ready = False


def stats():
    return dict(_stats)


def reset_stats():
    for k in _stats:
        _stats[k] = 0


def _take(n):
    """Token bucket: block until n tokens are available."""
    n = min(n, TOKENS_PER_MIN)
    while True:
        with _lock:
            now = time.monotonic()
            _bucket["tokens"] = min(TOKENS_PER_MIN, _bucket["tokens"] + (now - _bucket["at"]) * TOKENS_PER_MIN / 60)
            _bucket["at"] = now
            if _bucket["tokens"] >= n:
                _bucket["tokens"] -= n
                return
            wait = (n - _bucket["tokens"]) * 60 / TOKENS_PER_MIN
        time.sleep(min(wait, 2))


def ask(text, questions):
    """One request. questions: {key: {"type", "instructions", "criteria"?}}, at most 1,000. Returns the answers dict."""
    if not questions:
        return {}
    if not text.strip():
        raise ValueError("Decisions needs a non-empty text")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError("at most 1,000 questions per request")
    estimate = (len(text) + sum(len(json.dumps(q)) for q in questions.values())) // 3
    _take(estimate)
    body = {"model": MODEL, "input": text, "questions": questions}
    last = ""
    for attempt in range(5):
        try:
            r = requests.post(URL, headers={"x-goog-api-key": rag._key()}, json=body, timeout=90)
        except requests.RequestException as e:
            last = f"{type(e).__name__}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            data = r.json()
            used = data.get("usage", {}).get("total_input_tokens", estimate)
            with _lock:
                _stats["requests"] += 1
                _stats["tokens"] += used
                _bucket["tokens"] -= used - estimate      # correct the estimate
            return data["answers"]
        last = f"{r.status_code}: {r.text[:300]}"
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2.0 * (attempt + 1))
            continue
        break
    raise RuntimeError(f"Decisions API failed ({last})")


def _sha(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _claim_sha(claim_text):
    return _sha(_RUBRIC_SHA + claim_text)


def _ensure_table(cur):
    global _table_ready
    if not _table_ready:
        cur.execute("CREATE TABLE IF NOT EXISTS stance_cache (text_sha CHAR(40) NOT NULL, claim_sha CHAR(40) NOT NULL, "
                    "p0 FLOAT, p1 FLOAT, p2 FLOAT, p3 FLOAT, p4 FLOAT, PRIMARY KEY (text_sha, claim_sha))")
        _table_ready = True


def _stance(p):
    return {"stance": round(p[4] + 0.5 * p[3] - 0.5 * p[1] - p[0], 4), "presence": round(1 - p[2], 4)}


def probabilities(text, claims):
    """{claim_id: [p0..p4]} for every claim; only cache misses are sent to the API."""
    if not text.strip():            # a version with no readable text (redirect, templates only): the API rejects empty input, and nothing is addressed
        return {c["id"]: [0.0, 0.0, 1.0, 0.0, 0.0] for c in claims}
    tsha = _sha(text)
    shas = {c["id"]: _claim_sha(c["text"]) for c in claims}
    out = {}
    conn = rag.connect()
    conn.autocommit = True      # no snapshot held across the API call: parallel workers write the same table
    try:
        cur = conn.cursor()
        _ensure_table(cur)
        if claims:
            marks = ",".join("?" * len(claims))
            cur.execute(f"SELECT claim_sha, p0, p1, p2, p3, p4 FROM stance_cache WHERE text_sha = ? AND claim_sha IN ({marks})",
                        [tsha] + list(shas.values()))
            hit = {r[0]: list(r[1:]) for r in cur.fetchall()}
            for cid, s in shas.items():
                if s in hit:
                    out[cid] = hit[s]
        with _lock:
            _stats["cache_hits"] += len(out)
        missing = [c for c in claims if c["id"] not in out]
        if missing:
            keys = {f"q{i}": c for i, c in enumerate(missing)}
            answers = ask(text, {k: {"type": "score", "instructions": INSTRUCTIONS.format(claim=c["text"]), "criteria": LEVELS}
                                 for k, c in keys.items()})
            rows = []
            for k, c in keys.items():
                pr = answers[k]["probabilities"]
                p = [float(pr.get(str(i), 0.0)) for i in range(5)]
                out[c["id"]] = p
                rows.append((tsha, shas[c["id"]], *p))
            cur.executemany("INSERT IGNORE INTO stance_cache VALUES (?,?,?,?,?,?,?)", rows)
    finally:
        conn.close()
    return out


def score(text, claims):
    """How the text treats each claim. claims: [{"id", "text"}]. Cached and throttled."""
    return {cid: _stance(p) for cid, p in probabilities(text, claims).items()}


def score_many(jobs):
    """jobs: [(text, claims)]. Runs in the shared thread pool; results in the same order. Raises the first error."""
    return list(_pool.map(lambda j: score(*j), jobs))


def ask_many(jobs):
    """jobs: [(text, questions)] -> [answers], in order."""
    return list(_pool.map(lambda j: ask(*j), jobs))


# ---------------------------------------------------------------- validation

def _group(p):
    best = max(range(5), key=lambda i: p[i])
    return best, ("neg" if best < 2 else "absent" if best == 2 else "pos")


VALIDATE = [
    # (title, claim, expected level)  judged on the article's current text in table `article`
    ("Talvisota", "The Soviet Union attacked Finland in November 1939.", 4),
    ("Talvisota", "Finland started the Winter War by attacking the Soviet Union.", 0),
    ("Talvisota", "The Winter War ended with the Moscow Peace Treaty in March 1940.", 4),
    ("Talvisota", "Finland lost territory to the Soviet Union in the peace treaty.", 4),
    ("Talvisota", "Finnish artillery fired the shots at Mainila.", 1),
    ("Talvisota", "Finland won the Winter War and gained territory.", 0),
    ("Talvisota", "The Soviet Union was expelled from the League of Nations because of the attack.", 4),
    ("Talvisota", "Sweden joined the war as a combatant state on Finland's side.", 0),
    ("Talvisota", "The Eurovision Song Contest was held in Helsinki.", 2),
    ("Talvisota", "Finland adopted the euro as its currency.", 2),
    ("Suomen sisällissota", "The war was fought between the Reds and the Whites.", 4),
    ("Suomen sisällissota", "The Whites won the war.", 4),
    ("Suomen sisällissota", "The Reds won the war.", 0),
    ("Suomen sisällissota", "German troops fought on the side of the Whites.", 4),
    ("Suomen sisällissota", "The war has been called by several different names, such as war of liberation and class war.", 4),
    ("Suomen sisällissota", "More Reds than Whites died in the war and its aftermath.", 4),
    ("Suomen sisällissota", "Many Red prisoners died in prison camps after the war.", 4),
    ("Suomen sisällissota", "The war took place in 1939.", 0),
    ("Suomen sisällissota", "Finland joined NATO in 2023.", 2),
    ("Suomen sisällissota", "Nokia was the largest mobile phone maker in the world.", 2),
    ("Jatkosota", "Finland fought against the Soviet Union alongside Germany.", 4),
    ("Jatkosota", "The Continuation War ended with the Moscow Armistice in 1944.", 4),
    ("Jatkosota", "Finland had to cede Petsamo to the Soviet Union.", 4),
    ("Jatkosota", "Finland was a neutral country throughout the Second World War.", 0),
    ("Jatkosota", "Finnish troops occupied East Karelia.", 4),
    ("Jatkosota", "Finland's war was a separate war, independent of Germany's war.", 1),
    ("Jatkosota", "Finland occupied Moscow during the war.", 0),
    ("Jatkosota", "After the armistice Finland had to drive German troops out of Lapland.", 4),
    ("Jatkosota", "The 1952 Summer Olympics were held in Helsinki.", 2),
    ("Jatkosota", "Finland became a member of the European Union in 1995.", 2),
]


def validate():
    conn = rag.connect()
    cur = conn.cursor()
    by_title = {}
    for title, claim, level in VALIDATE:
        by_title.setdefault(title, []).append((claim, level))
    ok3 = ok5 = n = 0
    t0 = time.perf_counter()
    for title, items in by_title.items():
        cur.execute("SELECT text FROM article WHERE lang = 'fi' AND title = ?", (title,))
        row = cur.fetchone()
        if not row:
            print(f"missing article: {title}")
            continue
        text = row[0][:16000]
        claims = [{"id": str(i), "text": c} for i, (c, _) in enumerate(items)]
        probs = probabilities(text, claims)
        for i, (claim, level) in enumerate(items):
            p = probs[str(i)]
            got, g = _group(p)
            want = "neg" if level < 2 else "absent" if level == 2 else "pos"
            n += 1; ok5 += got == level; ok3 += g == want
            mark = "ok " if g == want else "BAD"
            print(f"{mark} {title[:20]:20} want {level} got {got}  stance {_stance(p)['stance']:+.2f} presence {_stance(p)['presence']:.2f}  {claim}")
    conn.close()
    print(f"\nthree-way agreement {ok3}/{n} = {ok3 / n:.0%}; exact level {ok5}/{n} = {ok5 / n:.0%}; "
          f"{(time.perf_counter() - t0) * 1000:.0f} ms; {stats()}")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["probe"]:
        t0 = time.perf_counter()
        r = requests.post(URL, headers={"x-goog-api-key": rag._key()}, timeout=60, json={
            "model": MODEL, "input": "Talvisota alkoi, kun Neuvostoliitto hyökkäsi Suomeen 30. marraskuuta 1939.",
            "questions": {"attack": {"type": "noul", "instructions": "Does the text say the Soviet Union attacked Finland?"},
                          "claim": {"type": "score", "instructions": INSTRUCTIONS.format(claim="Finland started the war."), "criteria": LEVELS}}})
        print(r.status_code, f"{(time.perf_counter() - t0) * 1000:.0f} ms")
        print(json.dumps(r.json(), indent=1, ensure_ascii=False))
    elif sys.argv[1:] == ["validate"]:
        validate()
    else:
        print(__doc__)
