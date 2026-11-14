"""POST /api/ask: find passages by meaning, then have the LLM answer ONLY from them, with [n] citations.

Steps:
  1. search() finds the nearest articles (any language); sv/en hits are mapped to the Finnish
     article on the same topic via wikidata_id. Keep the top 5 distinct articles.
  2. Text of those articles:
       - now:   the article table (dataset S).
       - as_of: the version that was live on that date, from article_version
                FOR SYSTEM_TIME AS OF (dataset M, only articles whose history is loaded), wikitext
                cleaned to plain text with mwparserfromhell.
  3. Split into paragraphs, embed each as "<title>: <paragraph>" with bge-m3 (same Ollama call
     as search.py), keep the 8 closest to the question. Current paragraphs are cached in chunk /
     chunk_vector per rev_id; historic ones are NOT (that cache is keyed to the current
     revision) and only kept in memory.
  With RETRIEVAL=chunk (not for as_of), steps 1-3 become: the 40 nearest paragraphs straight from
  chunk_vector (filled by eval/build_chunks.py), plus the paragraphs of any article-first hit not
  chunked yet; best 8, at most 3 per article.
  With RETRIEVAL=fusion (not for as_of), step 1 ranks articles by reciprocal rank fusion of the
  article-first top 20 and the chunk-first top 20 (api/search.py fused_search); steps 2-3 as above.
  With QUERY_REWRITE=1 (article and fusion), step 1 starts with one short call to the review model
  (api/rewrite.py): up to 3 Finnish search queries (historical names, synonyms, one per likely
  event). The question and each query are searched, the lists merged by reciprocal rank fusion, and
  a paragraph scores its best cosine to the question or any query. The queries are in the trace.
  4. The LLM answers from the passages only, citing [1], [2], ... in the request's `lang`, else
     the question's language (api/language.py); an answer in another language is retried once
     with a stronger instruction. Several matching events: one line each, in date order.
Returns the shape in docs/API_CONTRACT.md: answer, citations, sql, took_ms. Pass a dict as
`trace` to also get what happened at each step (used by /api/ask?debug=1 and the test page).
"""
import copy
import json
import math
import os
import re
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import date, datetime

import mwparserfromhell

from . import rewrite as rewrite_mod
from .language import detect as detect_language
from .llm import chat, model_name
from .search import (articles_from_chunks, connect, embed_texts, fused_search, rrf_merge, search,
                     search_chunks)

N_ARTICLES = 5
N_PASSAGES = 8
SEARCH_CANDIDATES = 20       # fetch extra hits: several languages of one topic collapse into one
MIN_PARAGRAPH_CHARS = 40     # drop one-word lines and short list items
HEADING = re.compile(r"^#{1,6}\s")  # markdown headings in dataset S's text: never a passage
MAX_PER_ARTICLE = 3          # chunk-first: at most this many of the passages from one article
CHUNK_CANDIDATES = 40        # chunk-first: nearest chunks the vector index returns
MAX_PARAGRAPH_CHARS = 1500   # split very long paragraphs so one passage stays readable
EMBED_BATCH = 64
LLM_PASSAGE_CHARS = 800      # each passage is trimmed to about this before it goes to the LLM
HISTORY_MEMORY = 30          # historic article versions whose paragraph vectors stay in memory
# Bibliography / "files on Commons" lines: never useful as evidence, and they crowd out real text.
REFERENCE_LINE = re.compile(r"\bISBN\b|\bISSN\b|Wikimedia Commons|^Kuvia tai muita tiedostoja", re.I)

FI_BY_WIKIDATA_SQL = """
SELECT wikidata_id, page_id FROM article WHERE lang = 'fi' AND wikidata_id IN ({ph})"""

ARTICLE_TEXT_SQL = """
SELECT lang, page_id, rev_id, title, url, text FROM article WHERE (lang, page_id) IN ({ph})"""

# The version of each article that was live at the given moment (UTC).
HISTORY_AS_OF_SQL = """
SELECT page_id, rev_id, title, row_start, text
FROM article_version FOR SYSTEM_TIME AS OF TIMESTAMP %s
WHERE page_id IN ({ph})"""

# Which of the articles have any history loaded, and since when (uses KEY page_start, db/004).
HISTORY_FIRST_SQL = """
SELECT page_id, MIN(row_start) FROM article_version FOR SYSTEM_TIME ALL
WHERE page_id IN ({ph}) GROUP BY page_id"""

SYSTEM_PROMPT = """You answer questions about the history of Finland using ONLY the numbered \
passages from Wikipedia that you are given.

Rules:
- Use only facts stated in the passages. Do not use outside knowledge, and do not guess.
- After every sentence that uses a passage, cite it with its number in square brackets, e.g. [1] or [2][3].
  Only cite numbers that appear in the passages.
- If the passages do not contain the answer, say clearly that the sources available do not answer \
the question (you may say what related information they do contain, with citations). Do not guess.
- Write in a neutral, encyclopedic tone. Do not repeat loaded or emotional wording from the \
passages (labels such as "bandits", "butchers", "heroic", "glorious", or one side's name for an \
event); use neutral terms, and keep such a term only when the passage attributes it to someone.
- Attribute contested claims and evaluations to who makes them ("according to ...", "X stated \
that ..."), not as plain fact.
- If the passages disagree (for example on a number or a date), say so and give both, each with \
its citation, instead of choosing one.
- Write the answer in the SAME language as the question (Finnish, Swedish, English, ...), even \
though the passages are mostly in Finnish.
- If the passages describe several DIFFERENT events that each fit the question (for example \
several wars, invasions or occupations), first say in one sentence that the question can mean \
more than one event, then list each event on its own line, in date order, as \
"- <date or years>: <what happened, briefly> [n]". List only events the passages describe.
- Otherwise be concise: answer in at most 3 sentences. Do not turn a question with one clear \
answer into a list."""

AS_OF_NOTE = ("\n- The passages are Wikipedia as it stood on {as_of}. Answer as those passages "
              "describe it, even if later knowledge differs.")

ANSWER_LANGUAGES = {"en": "English", "fi": "Finnish", "sv": "Swedish"}
SAME_LANGUAGE_RULE = ("- Write the answer in the SAME language as the question (Finnish, Swedish, English, ...), even \
though the passages are mostly in Finnish.")
assert SAME_LANGUAGE_RULE in SYSTEM_PROMPT
# When the request sets `lang`, this REPLACES the rule above (two conflicting rules: models follow the question).
LANG_RULE = ("- Write the answer in {language}, whatever the language of the question or the passages. "
             "Translate the facts from the passages into {language}.")
# Sent once more when the answer has no [n] citation at all.
CITE_RETRY = ("Your answer has no citations. Write the same answer again, in the same language, with "
              "the number of the passage in square brackets after every sentence that uses it, e.g. [1]. "
              "If the passages do not answer the question, say so.")
# Sent once more when the first answer came back in another language (checked with api/language.py).
LANG_RETRY = ("IMPORTANT: your previous answer was not in {language}. Write the WHOLE answer in "
              "{language} only. Do not write in {others}. Translate the facts from the passages; "
              "keep the [n] citations and proper names.")

MODES = ("reader", "editor")
EDITOR_PASSAGES = 12         # editor mode: more passages ...
EDITOR_MIN_ARTICLES = 4      # ... from at least this many different articles (when there are)
REVIEW_TIMEOUT_S = float(os.getenv("REVIEW_TIMEOUT_S", "30"))
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="review")


class _Timer:
    """Collects how long each step took, in ms: with timer("step"): ..."""

    def __init__(self):
        self.ms: dict[str, int] = {}

    @contextmanager
    def __call__(self, step: str):
        t = time.perf_counter()
        try:
            yield
        finally:
            self.ms[step] = self.ms.get(step, 0) + round((time.perf_counter() - t) * 1000)


def answer(question: str, as_of: str | None = None, timings: dict | None = None,
           trace: dict | None = None, mode: str = "reader", lang: str | None = None) -> dict:
    """Answer a question, optionally as Wikipedia stood on `as_of` (YYYY-MM-DD, UTC midnight).

    mode "reader" (default): the contract shape; each citation also gets neutrality {level, reason}.
    mode "editor": 12 passages from at least 4 articles; each citation gets the full neutrality
    review and an edit_url, and the response gets `conflicts` (passages of different articles that
    disagree). The neutrality review (api/review.py) runs in parallel with the answer.
    `timings` gets the ms per step; `trace` gets the timings plus what each step found."""
    t0 = time.perf_counter()
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    lang = (lang or "").strip().lower() or None
    if lang and lang not in ANSWER_LANGUAGES:
        raise ValueError(f"lang must be one of {', '.join(ANSWER_LANGUAGES)}")
    editor = mode == "editor"
    as_of_ts = _parse_as_of(as_of) if as_of else None

    timer = _Timer()
    tr: dict = {"as_of": as_of or None, "mode": mode, "model": os.getenv("LLM_MODEL"),
                "reasoning_effort": os.getenv("LLM_REASONING_EFFORT", "none"), "skipped": []}
    reviewed = None
    try:
        passages, sql, message = retrieve(
            question, as_of, timer, tr,
            n_passages=EDITOR_PASSAGES if editor else N_PASSAGES,
            min_articles=EDITOR_MIN_ARTICLES if editor else 1, rewrite=rewrite_mod.enabled())
        if message:
            return _finish(_result(message, [], sql, t0), editor)

        from . import review
        review_started = time.perf_counter()
        review_job = _pool.submit(review.review_passages, passages, conflicts=editor,
                                  lang=ANSWER_LANGUAGES.get(lang))
        tr["review_model"] = review.review_model()
        tr["lang"] = lang

        # The answer language: the request's `lang`, else the question's (detected); None = unsure,
        # then the prompt's "same language as the question" rule stays and nothing is checked.
        target = lang or detect_language(question)
        system = SYSTEM_PROMPT
        if target:
            system = system.replace(SAME_LANGUAGE_RULE, LANG_RULE.format(language=ANSWER_LANGUAGES[target]))
        system += AS_OF_NOTE.format(as_of=as_of) if as_of_ts else ""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _user_prompt(question, passages) + (
                f"\n\nWrite the answer in {ANSWER_LANGUAGES[target]}." if target else "")},
        ]
        with timer("llm"):
            reply = chat(messages)
        got = detect_language(reply)
        tr["answer_language"] = {"target": target, "from": "request" if lang else ("question" if target else None),
                                 "detected": got, "retried": False}
        if target and got and got != target:  # wrong language: once more, with a stronger instruction
            language = ANSWER_LANGUAGES[target]
            others = " or ".join(v for k, v in ANSWER_LANGUAGES.items() if k != target)
            with timer("llm_retry"):
                retry = chat(messages + [{"role": "assistant", "content": reply},
                                         {"role": "user", "content": LANG_RETRY.format(language=language, others=others)}])
            tr["answer_language"].update(retried=True, first_detected=got, detected=detect_language(retry))
            reply = retry
        valid = {str(n) for n in range(1, len(passages) + 1)}
        if not valid & set(re.findall(r"\[(\d+)\]", reply)):  # no citation at all: once more
            with timer("llm_cite_retry"):
                retry = chat(messages + [{"role": "assistant", "content": reply},
                                         {"role": "user", "content": CITE_RETRY}])
            tr["citation_retry"] = True
            if valid & set(re.findall(r"\[(\d+)\]", retry)):
                reply = retry
        with timer("review_wait"):  # the latency the review adds: only what is left after the answer
            reviewed, review_error = _await_review(review_job, review_started)
        if reviewed:
            timer.ms["review"] = reviewed["ms"]
        tr["review"] = {"error": review_error,
                        "llm_call": reviewed["llm_call"] if reviewed else None,
                        "flagged": sorted(n for n, j in (reviewed or {}).get("by_n", {}).items()
                                          if j["level"] == "flagged")}
    finally:
        timer.ms["total"] = round((time.perf_counter() - t0) * 1000)
        print(f"[ask] {question[:60]!r} as_of={as_of} mode={mode} model={os.getenv('LLM_MODEL')} "
              f"timings ms: {timer.ms}", file=sys.stderr, flush=True)
        if timings is not None:
            timings.update(timer.ms)
        if trace is not None:
            tr["timings_ms"] = timer.ms
            trace.update(tr)

    by_n = reviewed["by_n"] if reviewed else {}
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", reply)}
    citations = []
    for n, p in enumerate(passages, 1):
        if n not in cited:
            continue
        c = {"n": n, "page_id": p["page_id"], "rev_id": p["rev_id"], "title": p["title"],
             "url": p["url"], "passage": p["text"], "cited": True}
        j = by_n.get(n)
        if editor:
            c["neutrality"] = j
            c["edit_url"] = edit_url(p)
        else:
            c["neutrality"] = {"level": j["level"], "reason": j.get("reason")} if j else None
        citations.append(c)
    if trace is not None:
        trace["passages"] = [{"n": n, "title": p["title"], "rev_id": p["rev_id"],
                              "score": round(p["score"], 4), "cited": n in cited,
                              "neutrality": by_n.get(n), "text": p["text"]}
                             for n, p in enumerate(passages, 1)]
    result = _result(reply, citations, sql, t0)
    if editor:
        # A copy: the review caches its conflict objects, and the evidence is added to them.
        result["conflicts"] = copy.deepcopy(reviewed["conflicts"]) if reviewed else None
        # Passages the review flagged that the answer did not cite: still worth an editor's look.
        result["flagged_passages"] = [
            {"n": n, "page_id": p["page_id"], "rev_id": p["rev_id"], "title": p["title"],
             "url": p["url"], "passage": p["text"], "neutrality": by_n[n], "edit_url": edit_url(p)}
            for n, p in enumerate(passages, 1)
            if n not in cited and (by_n.get(n) or {}).get("level") == "flagged"]
        # "Evidence for the editor to check" on each conflict side and each flagged passage
        # (cited or not): references, agreeing passages, other languages. No verdict.
        from .evidence import add_evidence, items_for_ask
        flagged = [c for c in citations if (c.get("neutrality") or {}).get("level") == "flagged"]
        with timer("evidence"):
            add_evidence(items_for_ask(result["conflicts"], flagged + result["flagged_passages"],
                                       passages), passages)
        timer.ms["total"] = result["took_ms"] = round((time.perf_counter() - t0) * 1000)
    return result


def search_articles(question: str, q_vec, query_vecs: list, mode: str, limit: int = SEARCH_CANDIDATES):
    """The article list for retrieval (also used by eval/run_eval.py). With rewritten queries
    (query_vecs), the question and each query are searched and the lists merged by reciprocal rank
    fusion. Returns (rows, sql)."""
    def one(v):
        return fused_search(v, limit=limit) if mode == "fusion" else search(question, limit=limit, vector=v)

    hits, sql = one(q_vec)
    if query_vecs:
        hits = rrf_merge([hits] + [one(v)[0] for v in query_vecs])
        # Each rewritten query's best article keeps a place at the top (in RRF order): with one
        # query per likely event, an event found by one query only must not be outvoted by an
        # article that several queries find.
        pinned = [h for h in hits if any(r == 1 for r in h["ranks"][1:])]
        hits = (pinned + [h for h in hits if h not in pinned])[:limit]
        sql = (f"-- QUERY_REWRITE: the question and {len(query_vecs)} rewritten queries are each searched "
               "with the SQL below;\n-- the article lists are merged by reciprocal rank fusion (k = 60) "
               "in Python.\n" + sql.strip())
    return hits, sql


def retrieve(question: str, as_of: str | None, timer: "_Timer", tr: dict,
             n_passages: int = N_PASSAGES, min_articles: int = 1, q_vec=None, rewrite: bool = False):
    """The passages for a question (also used by POST /api/check).
    rewrite=True: first rewrite the question into Finnish search queries (api/rewrite.py), search
    with each, and rank paragraphs by their best match to the question or any query.
    Returns (passages, sql, message): message is set when there is nothing to answer from."""
    as_of_ts = _parse_as_of(as_of) if as_of else None
    mode = "article" if as_of_ts else retrieval_mode()
    queries = []
    if rewrite and mode != "chunk":
        with timer("rewrite"):
            rw = rewrite_mod.rewrite_queries(question)
        queries = rw["queries"]
        tr["rewrite"] = {"queries": queries, "ms": rw["ms"], "cached": rw["cached"], "error": rw["error"],
                         "model": model_name("review")}
    to_embed = ([question] if q_vec is None else []) + queries
    with timer("embed_question"):
        vecs = embed_texts(to_embed) if to_embed else []
    if q_vec is None:
        q_vec, vecs = vecs[0], vecs[1:]
    query_vecs = vecs
    with timer("search_sql"):
        hits, search_sql = search_articles(question, q_vec, query_vecs, mode)
        with connect() as con, con.cursor() as cur:
            keys = _top_articles(cur, hits)
            texts = _article_texts(cur, keys)
    arts = [texts[k] for k in keys if k in texts]  # search order: ties favour the better article
    tr["search_hits"] = [{"lang": h["lang"], "page_id": h["page_id"], "title": h["title"],
                          "distance": round(float(h["distance"]), 4),
                          **({"rrf": round(h["rrf"], 5), "ranks": h.get("ranks")} if "rrf" in h else {})}
                         for h in hits[:10]]
    sql = search_sql.strip()

    if as_of_ts:
        with timer("history_sql"):
            arts, history_sql = _historic_versions(arts, as_of_ts, tr.setdefault("skipped", []))
        sql += "\n\n-- then the version of each article live on " + as_of + ":\n" + history_sql
        if not arts:
            tr["articles"] = []
            return [], sql, _no_history_message(as_of, tr["skipped"])
    tr["articles"] = [{"lang": a["lang"], "page_id": a["page_id"], "title": a["title"],
                       "rev_id": a["rev_id"], "source": "history" if as_of_ts else "current",
                       "version_from": a.get("version_from")} for a in arts]

    tr["retrieval"] = mode
    if tr["retrieval"] == "chunk":
        candidates, chunk_sql, unchunked = _chunk_first(q_vec, arts, timer, tr)
        passages = _select(candidates, n_passages, MAX_PER_ARTICLE, min_articles)
        lang_of = {a["page_id"]: a["lang"] for a in unchunked}
        used = {p["page_id"]: p for p in passages}
        tr["articles"] = [{"lang": lang_of.get(pid, "fi"), "page_id": pid, "title": p["title"],
                           "rev_id": p["rev_id"], "source": p["via"], "version_from": None}
                          for pid, p in used.items()]
        sql = chunk_sql.strip() + (
            "\n\n-- also article-first (the query below), whose articles not chunked yet were "
            "split and embedded now: " + ", ".join(a["title"] for a in unchunked) + "\n" + sql
            if unchunked else "")
    else:
        candidates = _best_passages(q_vec, arts, timer, use_cache=not as_of_ts, extra_vecs=query_vecs)
        # several rewritten queries: at most 3 passages per article, so one event cannot fill all
        passages = _select(candidates, n_passages, MAX_PER_ARTICLE if len(query_vecs) > 1 else None,
                           min_articles)
    if not passages:
        return [], sql, "No matching articles were found for this question."
    return passages, sql, None


def _select(candidates: list[dict], n: int, max_per_article: int | None, min_articles: int) -> list[dict]:
    """The n best passages (candidates are sorted best first). The same text only once; at most
    max_per_article from one article; and first the best passage of each of the top
    min_articles articles, so they come from at least that many articles when there are."""
    unique, seen = [], set()
    for c in candidates:
        if not is_prose(c["text"]):  # table rows already in the chunk cache
            continue
        if (c["page_id"], c["text"]) not in seen:
            seen.add((c["page_id"], c["text"]))
            unique.append(c)
    chosen, taken, per = [], set(), {}
    if min_articles > 1:
        for i, c in enumerate(unique):
            if c["page_id"] not in per:
                chosen.append(c)
                taken.add(i)
                per[c["page_id"]] = 1
                if len(chosen) == min(min_articles, n):
                    break
    for i, c in enumerate(unique):
        if len(chosen) == n:
            break
        if i in taken or (max_per_article and per.get(c["page_id"], 0) >= max_per_article):
            continue
        chosen.append(c)
        per[c["page_id"]] = per.get(c["page_id"], 0) + 1
    return sorted(chosen, key=lambda c: c["score"], reverse=True)


def _await_review(job, started):
    """(review, error). Waits at most REVIEW_TIMEOUT_S from when the review started (slow retrieval does not use it up); a late
    review still finishes in the background and fills the cache for next time."""
    try:
        return job.result(timeout=max(0.1, REVIEW_TIMEOUT_S - (time.perf_counter() - started))), None
    except FutureTimeout:
        return None, f"review took longer than {REVIEW_TIMEOUT_S:.0f} s"
    except Exception as e:  # the review must never fail the answer
        print(f"[ask] review failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None, f"{type(e).__name__}: {e}"


def edit_url(p: dict) -> str:
    return f"https://{p.get('lang', 'fi')}.wikipedia.org/w/index.php?curid={p['page_id']}&action=edit"


def _finish(result: dict, editor: bool) -> dict:
    if editor:
        result["conflicts"] = []
        result["flagged_passages"] = []
    return result


def retrieval_mode() -> str:
    """RETRIEVAL=chunk: passages straight from the paragraph vectors (eval/build_chunks.py).
    RETRIEVAL=article (default): nearest articles first, then their paragraphs.
    RETRIEVAL=fusion: articles by reciprocal rank fusion of both lists, then paragraphs as article.
    as_of always uses article-first, because chunks exist only for the current revisions."""
    mode = os.getenv("RETRIEVAL", "article").strip().lower()
    return mode if mode in ("article", "chunk", "fusion") else "article"


CHUNKED_SQL = """
SELECT page_id, MIN(rev_id), MAX(rev_id) FROM ask_chunk WHERE page_id IN ({ph}) GROUP BY page_id"""


def _chunk_first(q_vec, arts: list[dict], timer: "_Timer", tr: dict):
    """Chunk-first: the CHUNK_CANDIDATES nearest paragraphs. Articles that article-first search
    found but that are not chunked yet at their current revision (and sv/en articles, never
    chunked) are split and embedded now, as before, and compete on the same cosine score.
    Returns (candidates best first, chunk SQL, the articles embedded now)."""
    with timer("chunk_search"):
        chunks, chunk_sql = search_chunks(q_vec, CHUNK_CANDIDATES)
        fi_ids = [a["page_id"] for a in arts if a["lang"] == "fi"]
        chunked = set()
        if fi_ids:
            with connect() as con, con.cursor() as cur:
                cur.execute(CHUNKED_SQL.format(ph=",".join(["%s"] * len(fi_ids))), fi_ids)
                revs = {pid: (lo, hi) for pid, lo, hi in cur.fetchall()}
            chunked = {a["page_id"] for a in arts
                       if a["lang"] == "fi" and revs.get(a["page_id"]) == (a["rev_id"], a["rev_id"])}
    unchunked = [a for a in arts if not (a["lang"] == "fi" and a["page_id"] in chunked)]

    candidates = [{"lang": "fi", "page_id": c["page_id"], "rev_id": c["rev_id"], "title": c["title"],
                   "url": c["url"], "text": c["text"], "score": 1 - float(c["distance"]), "via": "chunk"}
                  for c in chunks]
    timer.ms["paragraphs"] = len(candidates)
    timer.ms["paragraphs_from_cache"] = len(candidates)
    if unchunked:
        candidates += [{**c, "via": "embedded now"} for c in _best_passages(q_vec, unchunked, timer)]

    tr["chunk_hits"] = [{"lang": a["lang"], "page_id": a["page_id"], "title": a["title"],
                         "distance": round(float(a["distance"]), 4)}
                        for a in articles_from_chunks(chunks)[:10]]
    tr["unchunked"] = [a["title"] for a in unchunked]
    return sorted(candidates, key=lambda c: c["score"], reverse=True), chunk_sql, unchunked


def _result(text, citations, sql, t0):
    return {"answer": text, "citations": citations, "sql": sql.strip(),
            "took_ms": round((time.perf_counter() - t0) * 1000)}


def _parse_as_of(as_of: str) -> str:
    try:
        d = date.fromisoformat(as_of.strip()[:10])
    except ValueError:
        raise ValueError(f"as_of must be a date like 2015-06-01, got {as_of!r}") from None
    return f"{d.isoformat()} 00:00:00"


def _top_articles(cur, hits: list[dict]) -> list[tuple[str, int]]:
    """Up to N_ARTICLES distinct topics as (lang, page_id), Finnish version preferred."""
    wikidata = sorted({h["wikidata_id"] for h in hits if h["lang"] != "fi" and h["wikidata_id"]})
    fi_by_wd = {}
    if wikidata:
        cur.execute(FI_BY_WIKIDATA_SQL.format(ph=",".join(["%s"] * len(wikidata))), wikidata)
        fi_by_wd = dict(cur.fetchall())

    chosen, seen_topics = [], set()
    for h in hits:  # already ordered by distance
        topic = h["wikidata_id"] or (h["lang"], h["page_id"])
        if topic in seen_topics:
            continue
        seen_topics.add(topic)
        if h["lang"] == "fi":
            chosen.append(("fi", h["page_id"]))
        elif h["wikidata_id"] in fi_by_wd:
            chosen.append(("fi", fi_by_wd[h["wikidata_id"]]))
        else:
            chosen.append((h["lang"], h["page_id"]))  # no Finnish version: keep the original
        if len(chosen) == N_ARTICLES:
            break
    return chosen


def _article_texts(cur, keys: list[tuple[str, int]]) -> dict:
    if not keys:
        return {}
    cur.execute(ARTICLE_TEXT_SQL.format(ph=",".join(["(%s,%s)"] * len(keys))),
                [v for k in keys for v in k])
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {(r["lang"], r["page_id"]): r for r in rows}


# --- as_of: the versions live on a past date, from the system-versioned article_version (the site's history table, pacome/rag-demo/history.py). ---

def _historic_versions(arts: list[dict], as_of_ts: str, skipped: list) -> tuple[list[dict], str]:
    """Replace each article by its version live at as_of_ts. Articles without one go to `skipped`.
    Returns (versions, the AS OF query with its values filled in, for the sql field)."""
    fi_ids = [a["page_id"] for a in arts if a["lang"] == "fi"]
    for a in arts:
        if a["lang"] != "fi":
            skipped.append({"title": a["title"], "lang": a["lang"],
                            "reason": "history is only loaded for Finnish articles"})
    shown_sql = HISTORY_AS_OF_SQL.replace("%s", f"'{as_of_ts}'", 1).format(
        ph=", ".join(str(p) for p in fi_ids) or "NULL").strip()
    if not fi_ids:
        return [], shown_sql

    ph = ",".join(["%s"] * len(fi_ids))
    with connect() as con, con.cursor() as cur:
        cur.execute("SET time_zone = '+00:00'")  # row_start/row_end are Wikipedia's UTC times
        cur.execute(HISTORY_AS_OF_SQL.format(ph=ph), [as_of_ts, *fi_ids])
        live = {r[0]: r for r in cur.fetchall()}
        cur.execute(HISTORY_FIRST_SQL.format(ph=ph), fi_ids)
        first = dict(cur.fetchall())

    versions = []
    for a in arts:
        if a["lang"] != "fi":
            continue
        pid = a["page_id"]
        if pid in live:
            _, rev_id, title, row_start, wikitext = live[pid]
            versions.append({"lang": "fi", "page_id": pid, "rev_id": rev_id, "title": title,
                             "url": f"https://fi.wikipedia.org/?oldid={rev_id}",
                             "version_from": _iso(row_start), "text": wikitext or "",
                             "wikitext": True})
        elif pid in first:
            skipped.append({"title": a["title"], "lang": "fi",
                            "reason": f"did not exist yet (first version {_iso(first[pid])[:10]})"})
        else:
            skipped.append({"title": a["title"], "lang": "fi", "reason": "no history loaded"})
    return versions, shown_sql


def _no_history_message(as_of: str, skipped: list) -> str:
    why = "; ".join(f"{s['title']}: {s['reason']}" for s in skipped) or "no matching articles"
    return (f"I can't answer as of {as_of}: none of the articles that match this question has a "
            f"version from that date in the history table ({why}). History is loaded only for a "
            f"few showcase articles, so try without a date or with a later one.")


def _iso(v) -> str:
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, datetime) else str(v)


_DROP_TAGS = {"ref", "references", "gallery", "math", "table", "timeline", "imagemap", "score",
              "syntaxhighlight", "source", "nowiki", "noinclude"}
_DROP_LINK = re.compile(r"^\s*:?\s*(tiedosto|kuva|file|image|luokka|category|[a-z]{2,3}(-[a-z]+)?)\s*:", re.I)


def clean_wikitext(wikitext: str) -> str:
    """Wikitext -> plain text: drop references, tables, files, categories and interlanguage links;
    keep the visible text of links; templates (infoboxes etc.) are dropped by strip_code."""
    code = mwparserfromhell.parse(wikitext)
    for node in list(code.filter_tags()):
        if str(node.tag).lower() in _DROP_TAGS:
            try:
                code.remove(node)
            except ValueError:  # already removed with a parent
                pass
    for link in list(code.filter_wikilinks()):
        if _DROP_LINK.match(str(link.title)):
            try:
                code.remove(link)
            except ValueError:
                pass
    text = code.strip_code(normalize=True, collapse=True)
    return re.sub(r"\{\|.*?\|\}", "", text, flags=re.S)  # wiki tables the parser left behind


MIN_LETTER_SHARE = 0.4   # below this a "paragraph" is a table row of dashes and numbers


def is_prose(text: str) -> bool:
    """False for Markdown table rows that are mostly | --- | 151 | 149 |: nothing to cite in them.
    Text-like table rows (letters >= 40% of the characters) still count."""
    return sum(ch.isalpha() for ch in text) >= MIN_LETTER_SHARE * max(1, len(text))


def split_paragraphs(text: str) -> list[str]:
    paragraphs = []
    for block in re.split(r"\n\s*\n|\n", text):
        block = " ".join(block.split())
        if (len(block) < MIN_PARAGRAPH_CHARS or HEADING.match(block) or REFERENCE_LINE.search(block)
                or not is_prose(block)):
            continue
        while len(block) > MAX_PARAGRAPH_CHARS:
            cut = block.rfind(". ", 0, MAX_PARAGRAPH_CHARS)
            cut = cut + 1 if cut > MAX_PARAGRAPH_CHARS // 2 else MAX_PARAGRAPH_CHARS
            paragraphs.append(block[:cut].strip())
            block = block[cut:].strip()
        if len(block) >= MIN_PARAGRAPH_CHARS:
            paragraphs.append(block)
    return paragraphs


# Paragraph vectors of historic versions, in memory only: {(page_id, rev_id): [(text, vector)]}
_history_memory: "OrderedDict[tuple[int, int], list]" = OrderedDict()


def _best_passages(q_vec, arts: list[dict], timer: "_Timer", use_cache: bool = True,
                   extra_vecs=()) -> list[dict]:
    """Paragraphs of the articles, best first. A paragraph's score is its cosine to the question,
    or to a rewritten query (extra_vecs) when that is higher: each likely event gets its paragraphs."""
    if not arts:
        return []

    with connect() as con:
        cached: dict = {}
        if use_cache:
            with timer("cache_read"):
                cached = _cached_paragraphs(con, arts)
        else:
            for a in arts:
                hit = _history_memory.get((a["page_id"], a["rev_id"]))
                if hit is not None:
                    _history_memory.move_to_end((a["page_id"], a["rev_id"]))
                    cached[_cache_key(a, use_cache)] = hit

        todo = {}
        with timer("clean_split"):
            for a in arts:
                if _cache_key(a, use_cache) not in cached:
                    text = clean_wikitext(a["text"]) if a.get("wikitext") else a["text"]
                    todo[_cache_key(a, use_cache)] = (a["title"], split_paragraphs(text))
        flat = [(key, title, p) for key, (title, paras) in todo.items() for p in paras]
        with timer("embed_paragraphs"):
            vectors = embed_paragraphs([(title, p) for _, title, p in flat])
        fresh: dict = {}
        for (key, _, p), v in zip(flat, vectors):
            fresh.setdefault(key, []).append((p, v))

        if use_cache and chunk_write():
            with timer("cache_write"):
                _store_paragraphs(con, [a for a in arts if a["lang"] == "fi" and a["page_id"] in fresh],
                                  fresh)
        else:  # historic versions: never into chunk / chunk_vector, only into memory
            for key, paras in fresh.items():
                _history_memory[key] = paras
                while len(_history_memory) > HISTORY_MEMORY:
                    _history_memory.popitem(last=False)

    candidates = []
    for a in arts:
        key = _cache_key(a, use_cache)
        for text, vec in cached.get(key) or fresh.get(key, []):
            candidates.append({"lang": a["lang"], "page_id": a["page_id"], "rev_id": a["rev_id"],
                               "title": a["title"],
                               "url": a["url"], "text": text, "vec": vec})
    timer.ms["paragraphs"] = timer.ms.get("paragraphs", 0) + len(candidates)
    timer.ms["paragraphs_from_cache"] = (timer.ms.get("paragraphs_from_cache", 0)
                                         + sum(len(v) for v in cached.values()))
    with timer("rank_paragraphs"):
        for c in candidates:
            v = c.pop("vec")
            c["score"] = max([_cosine(q_vec, v)] + [_cosine(e, v) for e in extra_vecs])
        return sorted(candidates, key=lambda c: c["score"], reverse=True)


def embed_paragraphs(items: list[tuple[str, str]]) -> list[list[float]]:
    """Vectors for (article title, paragraph) pairs. The title is embedded with the paragraph
    ("Talvisota: Sota alkoi ...") so a paragraph that never names its subject still matches
    questions about it; only the plain paragraph is stored and shown."""
    vectors = []
    for i in range(0, len(items), EMBED_BATCH):
        vectors += embed_texts([f"{t}: {p}" for t, p in items[i:i + EMBED_BATCH]])
    return vectors


def write_chunks(cur, page_id: int, rev_id: int, paras: list[tuple[str, list[float]]]) -> None:
    """Replace one article's chunks. The caller owns the transaction."""
    cur.execute("DELETE FROM ask_chunk_vector WHERE page_id = %s", (page_id,))
    cur.execute("DELETE FROM ask_chunk WHERE page_id = %s", (page_id,))
    cur.executemany("INSERT INTO ask_chunk (page_id, chunk_no, rev_id, text) VALUES (%s, %s, %s, %s)",
                    [(page_id, n, rev_id, t) for n, (t, _) in enumerate(paras)])
    cur.executemany("INSERT INTO ask_chunk_vector (page_id, chunk_no, model, embedding) "
                    "VALUES (%s, %s, %s, VEC_FromText(%s))",
                    [(page_id, n, _embed_model(), json.dumps(v)) for n, (_, v) in enumerate(paras)])


# --- Paragraph cache in chunk / chunk_vector (db/001), current revisions only. ---
# chunk has no lang column, so page_id alone is the key: only Finnish articles are cached (the rare
# sv/en article without a Finnish version is embedded each time). A cached article is reused only
# while its rev_id matches the article row; a new revision replaces its chunks.

CACHE_READ_SQL = """
SELECT c.page_id, c.chunk_no, c.rev_id, c.text, VEC_ToText(v.embedding)
FROM ask_chunk c JOIN ask_chunk_vector v USING (page_id, chunk_no)
WHERE c.page_id IN ({ph}) AND v.model = %s
ORDER BY c.page_id, c.chunk_no"""


def _cache_key(art: dict, current: bool = True):
    if not current:
        return (art["page_id"], art["rev_id"])
    return art["page_id"] if art["lang"] == "fi" else (art["lang"], art["page_id"])


def chunk_write() -> bool:
    """CHUNK_WRITE=0: read the chunk cache but never write it (e.g. while eval/build_chunks.py
    runs, so the two cannot deadlock). Default 1."""
    return os.getenv("CHUNK_WRITE", "1").strip() != "0"


def _embed_model() -> str:
    return os.getenv("EMBED_MODEL", "bge-m3")


def _cached_paragraphs(con, arts: list[dict]) -> dict[int, list[tuple[str, list[float]]]]:
    """{fi page_id: [(text, vector), ...]} for articles whose chunks match their current rev_id."""
    fi = {a["page_id"]: a["rev_id"] for a in arts if a["lang"] == "fi"}
    if not fi:
        return {}
    rows: dict[int, list] = {}
    with con.cursor() as cur:
        cur.execute(CACHE_READ_SQL.format(ph=",".join(["%s"] * len(fi))), [*fi, _embed_model()])
        for page_id, _chunk_no, rev_id, text, vec in cur.fetchall():
            rows.setdefault(page_id, []).append((rev_id, text, vec))
    return {pid: [(t, json.loads(v)) for _, t, v in chunks]
            for pid, chunks in rows.items() if all(r == fi[pid] for r, _, _ in chunks)}


def _store_paragraphs(con, arts: list[dict], fresh: dict) -> None:
    """Replace each article's chunks with its freshly embedded paragraphs. Never fails the answer."""
    if not arts:
        return
    try:
        con.begin()
        with con.cursor() as cur:
            for a in arts:
                write_chunks(cur, a["page_id"], a["rev_id"], fresh[a["page_id"]])
        con.commit()
    except Exception as e:  # e.g. two requests caching the same article at once
        con.rollback()
        print(f"[ask] paragraph cache write skipped: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _trim(text: str, limit: int = LLM_PASSAGE_CHARS) -> str:
    """Cut to about `limit` chars, at a sentence end if one is near, else at a word."""
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    if cut < limit * 0.6:
        cut = text.rfind(" ", 0, limit)
    return text[:cut + 1].rstrip() + " …"


def _user_prompt(question: str, passages: list[dict]) -> str:
    # Only the LLM sees the trimmed text; citations still return the full passage.
    blocks = [f"[{n}] ({p['title']})\n{_trim(p['text'])}" for n, p in enumerate(passages, 1)]
    return "Passages:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"
