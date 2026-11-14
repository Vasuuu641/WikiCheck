"""Query rewriting (QUERY_REWRITE=1): before retrieval, one short call to the review model
(NEUTRALITY_* in .env, else the answer LLM) turns the question into up to 3 Finnish search queries:
one close to the question, plus the historical names and synonyms the Finnish articles use, and,
when the question can mean several events, one query per likely event. Retrieval searches with the
question and each query and merges the article lists by reciprocal rank fusion (api/ask.py).

Cached in memory per question. Any failure (timeout, bad JSON) means no rewriting: the question
is searched as it is.
"""
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict

from .llm import chat

MAX_QUERIES = 3
MAX_TOKENS = 200
CACHE_SIZE = 2000

PROMPT = """You turn a question about the history of Finland into search queries for the \
Finnish Wikipedia. Write at most 3 short search queries, IN FINNISH, whatever the language of \
the question.
- Use the names the Finnish articles use for the time in question, and synonyms: for example \
Venäjä, Venäjän keisarikunta, Neuvostoliitto (1922-1991); Ruotsi, Ruotsi-Suomi, Ruotsin \
valtakunta; Suomen suuriruhtinaskunta (1809-1917); the name of the war, treaty or event.
- If the question is about one clear thing: the first query stays close to the question \
(translated into Finnish), a second one may use the event's own name. One or two queries.
- If the question can mean several different events (for example several wars, invasions or \
occupations): the question itself is searched anyway, so use ALL the queries for distinct \
events, one query each. Think across every period (Swedish rule, the Grand Duchy, independent \
Finland) and pick the most important events, earliest first. Never two queries for the same event.
- A query for a named event is the event's Finnish Wikipedia article title as it most likely is \
(for example "Isoviha", "Suomen sota", "Talvisota"), with NO year added unless the title itself \
has one: a year makes the search find single battles of that year instead of the event.
Reply with JSON only, no other text: {"queries": ["...", "..."]}"""

_lock = threading.Lock()
_cache: "OrderedDict[str, list[str]]" = OrderedDict()


def enabled() -> bool:
    return os.getenv("QUERY_REWRITE", "0").strip() == "1"


def rewrite_queries(question: str) -> dict:
    """{queries: [up to 3 Finnish queries], ms, cached, error}. queries is [] on any failure."""
    t0 = time.perf_counter()
    key = " ".join(question.split()).lower()
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return {"queries": hit, "ms": 0, "cached": True, "error": None}
    try:
        reply = chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": question}],
                     temperature=0, max_tokens=MAX_TOKENS, role="review")
        m = re.search(r"\{.*\}", reply, re.S)
        data = json.loads(m.group(0)) if m else {}
        queries, seen = [], {key}
        for q in data.get("queries") or []:
            q = " ".join(str(q).split())
            if q and q.lower() not in seen and len(q) <= 200:
                seen.add(q.lower())
                queries.append(q)
        queries = queries[:MAX_QUERIES]
        error = None if queries else f"no queries in reply: {reply[:120]!r}"
    except Exception as e:  # the answer must not fail because rewriting did
        queries, error = [], f"{type(e).__name__}: {e}"
    if error:
        print(f"[rewrite] {question[:60]!r}: {error}", file=sys.stderr, flush=True)
    else:
        with _lock:
            _cache[key] = queries
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
    return {"queries": queries, "ms": round((time.perf_counter() - t0) * 1000), "cached": False,
            "error": error}
