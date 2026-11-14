"""Conflict finder: MariaDB vector search proposes article pairs, two LLMs cross-examine them.

Pipeline
1. Candidates  - (a) same wikidata_id in different languages, (b) nearest neighbours by
                 VEC_DISTANCE_COSINE (related articles).
2. Finder      - Gemini reads both articles and lists conflicts with EXACT quotes.
3. Verifier    - Verda (Kimi) is shown only the two quotes + sources and must confirm.
4. Hard check  - both quotes must literally occur in the source texts (kills hallucinations).
A conflict is `confirmed` only if it passes 3 and 4.
"""
import json
import os
import re
import unicodedata

import requests
from openai import OpenAI

from api.llm import google_key
from api.search import connect

CHARS = 7000
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
TYPES = ["number", "date", "chronology", "causal_framing", "naming", "scope_omission",
         "neutrality", "other"]

FIND_PROMPT = """You are a fact-checking editor for Wikipedia. Compare the two articles below and
list statements that CONFLICT: they cannot both be true, or they present the same fact with
different values (numbers, dates, order of events, causes, names, framing).
Ignore differences that are only wording, level of detail, or language.
Quotes MUST be copied verbatim (same language as the source) from the article text.

Return ONLY JSON: {{"conflicts":[{{"type":one of {types},"claim_a":"short english summary",
"quote_a":"verbatim from A","claim_b":"short english summary","quote_b":"verbatim from B",
"explanation":"why they conflict","confidence":0.0-1.0}}]}}
Return {{"conflicts":[]}} if there is no real conflict. Max 5 conflicts.

=== ARTICLE A: {ta} ({la}) ===
{a}

=== ARTICLE B: {tb} ({lb}) ===
{b}"""

VERIFY_PROMPT = """Two Wikipedia passages. Decide whether they genuinely CONFLICT about the same
fact (cannot both be true as written), as opposed to differing scope, detail, wording or
referring to different things.

Passage A ({ta}): "{qa}"
Passage B ({tb}): "{qb}"
Claimed conflict: {expl}

Reply ONLY JSON: {{"verdict":"conflict"|"not_conflict"|"unclear","reason":"one sentence"}}"""


def _json(text: str):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else {}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[\s\W_]+", " ", s).strip()


def quote_in(quote: str, source: str) -> bool:
    q = _norm(quote)
    return len(q) >= 8 and q in _norm(source)


def gemini_find(a: dict, b: dict) -> list[dict]:
    prompt = FIND_PROMPT.format(types=TYPES, ta=a["title"], la=a["lang"], a=a["text"][:CHARS],
                                tb=b["title"], lb=b["lang"], b=b["text"][:CHARS])
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": google_key()},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
        timeout=120)
    r.raise_for_status()
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _json(txt).get("conflicts", [])


def verda_verify(a: dict, b: dict, c: dict) -> dict:
    cl = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"], timeout=90)
    r = cl.chat.completions.create(
        model=os.environ["LLM_MODEL"], max_tokens=4096,
        messages=[{"role": "user", "content": VERIFY_PROMPT.format(
            ta=a["title"], tb=b["title"], qa=c.get("quote_a", ""), qb=c.get("quote_b", ""),
            expl=c.get("explanation", ""))}])
    m = r.choices[0].message
    try:
        return _json(m.content or getattr(m, "reasoning_content", "") or "")
    except Exception:
        return {"verdict": "unclear", "reason": "unparseable"}


def decide_verify(a: dict, b: dict, c: dict) -> dict:
    """Verify a conflict using the Gemini Decisions API (gemini-decision-1, 0 output tokens)."""
    text = (
        f"Passage A ({a['lang']}:{a['title']}): \"{c.get('quote_a', '')}\"\n"
        f"Passage B ({b['lang']}:{b['title']}): \"{c.get('quote_b', '')}\"\n"
        f"Claimed conflict: {c.get('explanation', '')}"
    )
    r = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/decisions",
        headers={"x-goog-api-key": google_key()},
        json={
            "model": "gemini-decision-1",
            "input": text,
            "questions": {
                "is_conflict": {
                    "type": "noul",
                    "instructions": (
                        "Do Passage A and Passage B genuinely CONFLICT about the same fact "
                        "(cannot both be true as written), rather than differing in scope, "
                        "detail, wording, or referring to different things?"
                    ),
                }
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    ans = r.json().get("answers", {}).get("is_conflict", {})
    p = float(ans.get("noul", 0.0))
    return {
        "verdict": "conflict" if p >= 0.65 else "not_conflict",
        "reason": f"gemini-decision-1 P(conflict)={p:.2f}",
        "p_conflict": round(p, 3),
    }


def examine(a: dict, b: dict, fast: bool = False) -> list[dict]:
    import time
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    cs = gemini_find(a, b)
    t_find = time.time() - t0
    for c in cs:
        c["quotes_found"] = quote_in(c.get("quote_a", ""), a["text"]) and \
            quote_in(c.get("quote_b", ""), b["text"])
    verify_fn = decide_verify if fast else verda_verify
    with ThreadPoolExecutor(max(1, len(cs))) as ex:  # verify all conflicts in parallel
        vs = list(ex.map(lambda c: verify_fn(a, b, c) if c["quotes_found"]
                         else {"verdict": "skipped"}, cs))
    for c, v in zip(cs, vs):
        c["verifier"] = v
        c["confirmed"] = c["quotes_found"] and v.get("verdict") == "conflict"
        c["t_find"], c["t_total"] = round(t_find, 1), round(time.time() - t0, 1)
    return cs


# ---------- candidate generation (MariaDB) ----------
def _rows(sql, args=()):
    with connect() as con, con.cursor() as cur:
        cur.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


COLS = "a.lang, a.page_id, a.rev_id, a.title, a.url, a.text, a.wikidata_id"


def crosslang_pairs(limit=50, offset=0):
    """Same Wikidata entity in two languages (en vs fi, sv vs fi...)."""
    rows = _rows(f"""SELECT {COLS} FROM article a JOIN (
        SELECT wikidata_id FROM article WHERE wikidata_id IS NOT NULL
        GROUP BY wikidata_id HAVING COUNT(DISTINCT lang) >= 2
        ORDER BY wikidata_id LIMIT %s OFFSET %s) w USING (wikidata_id)
        ORDER BY wikidata_id, lang""", (limit, offset))
    groups = {}
    for r in rows:
        groups.setdefault(r["wikidata_id"], []).append(r)
    for g in groups.values():
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                yield g[i], g[j], "crosslang"


NEIGHBOUR_SQL = f"""
SELECT {COLS}, v.distance FROM (
  SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding,
         (SELECT embedding FROM article_vector WHERE lang=%s AND page_id=%s)) AS distance
  FROM article_vector ORDER BY distance LIMIT %s) v
JOIN article a USING (lang, page_id) ORDER BY v.distance"""


def related_pairs(lang, page_id, k=6, max_dist=0.35):
    """Nearest neighbours of one article, excluding its own translations."""
    rows = _rows(NEIGHBOUR_SQL, (lang, page_id, k + 4))
    me = next(r for r in rows if r["lang"] == lang and r["page_id"] == page_id)
    for r in rows:
        if r is me or r["distance"] > max_dist or r["wikidata_id"] == me["wikidata_id"]:
            continue
        yield me, r, "related"


def find_conflicts(lang: str, page_id: int, k=6):
    """Everything we can say about one article: used by the future browser extension."""
    me = _rows(f"SELECT {COLS} FROM article a WHERE lang=%s AND page_id=%s", (lang, page_id))[0]
    pairs = list(related_pairs(lang, page_id, k))
    pairs += [(me, o, "crosslang") for o in _rows(
        f"SELECT {COLS} FROM article a WHERE wikidata_id=%s AND NOT (lang=%s AND page_id=%s)",
        (me["wikidata_id"], lang, page_id))] if me["wikidata_id"] else []
    res = []
    for a, b, kind in pairs:
        for c in examine(a, b):
            if c["confirmed"]:
                res.append({**c, "kind": kind,
                            "a": {"title": a["title"], "lang": a["lang"], "url": a["url"]},
                            "b": {"title": b["title"], "lang": b["lang"], "url": b["url"]}})
    return res
