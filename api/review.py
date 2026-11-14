"""Neutrality review of Wikipedia passages, for readers and editors. One LLM call per review, on
the "review" client (NEUTRALITY_* in .env, else the answer LLM; see api/llm.py).

review_passages(passages, conflicts=...) judges each passage's WORDING (not its topic):
    level       "ok" or "flagged"
    phrases     up to 3 exact phrases from the passage (checked: every phrase is in the text)
    category    loaded word | unsourced claim | emotional tone | one-sided
    reason      one line
    worst_sentence, suggestion   the worst sentence (exact) and a neutral rewording of it
  and, if asked, finds conflicts: two passages from DIFFERENT articles stating incompatible facts
  about the same thing, each with its exact phrase (checked against the text).

check_draft(text, passages, lang) does the same review for an editor's own draft sentence, lists
the draft's factual errors against the passages (a wrong date, year, number or name, each with the
exact phrase on both sides), and says which passages support or contradict it (POST /api/check).

Results are cached in memory by a hash of the passage text (the conflicts by the set of
passages), so asking again, or another question that retrieves the same paragraph, costs no call.
"""
import hashlib
import json
import re
import sys
import threading
import time
from collections import OrderedDict

from .llm import chat, settings

CATEGORIES = ("loaded word", "unsourced claim", "emotional tone", "one-sided")
CACHE_SIZE = 5000
MAX_TOKENS = 4000

RULES = """You review text from Wikipedia for neutral point of view, as an experienced Wikipedia \
editor would.

Judge the WORDING, not the topic. A war, a massacre, terror, executions or prison camps described \
in plain, factual words are "ok". Flag text only when its wording itself is a problem:
- loaded word: a value-laden label where a neutral term exists (e.g. calling one side "bandits", \
"butchers" or "heroes", or using one side's name for a contested event as if it were the only one)
- unsourced claim: an evaluation or contested statement given as plain fact with no attribution \
("it is clear that", "the greatest", "historians agree" without saying who)
- emotional tone: dramatic, sentimental or outraged language
- one-sided: only one party's view of a contested matter, presented as the truth
Most encyclopedia text is ok. When in doubt, it is ok. Quoting or attributing a view ("X called \
it ...", "according to Y ...") is ok."""

PASSAGES_TASK = """

You get numbered passages, each with its article.

Task A. For each passage number listed under "Judge", return an object:
  {"n": <number>, "level": "ok"}  or, only when the wording is a problem,
  {"n": <number>, "level": "flagged", "category": one of "loaded word" | "unsourced claim" | \
"emotional tone" | "one-sided",
   "phrases": [up to 3 short phrases copied EXACTLY, character for character, from the passage],
   "reason": "<one line>",
   "worst_sentence": "<the worst sentence, copied exactly>",
   "suggestion": "<that sentence reworded neutrally, in the passage's language, same facts>"}

Task B (only if "Find conflicts: yes"). Find pairs of passages from DIFFERENT articles that state \
incompatible facts about the same thing: a date, a number, a name or a cause. NOT conflicts: \
different scopes (battle deaths vs all deaths, one town vs the whole country, one year vs the \
whole war), rounded or approximate numbers, one passage giving more detail, different points in \
time, or two passages of the same article. Most sets have no conflicts; then return [].
  {"about": "<what they disagree on, one line>", "a": {"n": <number>, "phrase": "<exact phrase>"}, \
"b": {"n": <number>, "phrase": "<exact phrase>"}}

Reply with JSON only, no other text:
{"passages": [...], "conflicts": [...]}"""

DRAFT_TASK = """

An editor wants to add the DRAFT below to Wikipedia. You also get numbered passages from \
Wikipedia, each with its article.

Task A. Judge the DRAFT's wording by the rules above:
  {"level": "ok"}  or  {"level": "flagged", "category": ..., "phrases": [up to 3 exact phrases \
from the draft], "reason": "<one line>", "worst_sentence": "<exact>", "suggestion": "<neutral \
rewording, in the draft's language, same facts>"}

Task B. Fact-check the DRAFT against the passages. Compare EVERY date, year, number, name and \
place in the draft, character by character, with what the passages say about the same event. \
List each fact the draft gets wrong, even if the difference is small or looks like a typo \
(808 instead of 1808 is an error; 20 February instead of 21 February is an error):
  {"draft_phrase": "<exact wrong phrase from the draft>", "n": <passage number>, \
"passage_phrase": "<exact phrase from that passage with the right fact>", \
"problem": "<one line: what is wrong>", "fix": "<the whole draft with only that fact corrected>"}
Leaving out a detail is not an error. Rounding (about 300 for 312) is not an error. If nothing \
is wrong, give an empty list.

Task C. For EVERY passage say whether it supports or contradicts the draft's facts, or is \
unrelated (only on a related subject, or neither confirms nor denies it):
  {"n": <number>, "stance": "supports" | "contradicts" | "unrelated", \
"phrase": "<the exact phrase from the passage that supports or contradicts it, or empty>"}
A passage about the same event that gives a different date, year, number or name contradicts \
the draft. A broader or narrower scope does not.

Reply with JSON only, no other text:
{"draft": {...}, "facts": [...], "evidence": [...]}"""

_lock = threading.Lock()
_passage_cache: "OrderedDict[str, dict]" = OrderedDict()
_conflict_cache: "OrderedDict[frozenset, list]" = OrderedDict()


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _remember(cache: OrderedDict, key, value):
    with _lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > CACHE_SIZE:
            cache.popitem(last=False)


def review_model() -> str:
    s = settings("review")
    return s["model"] + ("" if s["source"] == "NEUTRALITY" else " (main LLM: NEUTRALITY_* not set)")


def review_passages(passages: list[dict], conflicts: bool = False, lang: str | None = None) -> dict:
    """passages: [{text, title, page_id, rev_id, url, lang?}] numbered 1.. in this order.
    lang: a language name ("English") to write the reasons in; None = the model's choice.
    Returns {"by_n": {n: judgement}, "conflicts": [...] or None, "llm_call": bool, "ms": int}."""
    t0 = time.perf_counter()
    hashes = [_h((lang or "") + "\x00" + p["text"]) for p in passages]  # reasons differ by lang
    with _lock:
        judged = {n: _passage_cache[h] for n, h in enumerate(hashes, 1) if h in _passage_cache}
        conf_key = frozenset(hashes)
        cached_conf = _conflict_cache.get(conf_key)
    judge = [n for n in range(1, len(passages) + 1) if n not in judged]
    distinct_articles = len({p["page_id"] for p in passages})
    want_conf = conflicts and cached_conf is None and distinct_articles > 1

    found_conf = cached_conf if conflicts else None
    if conflicts and distinct_articles <= 1:
        found_conf = []
    llm_call = bool(judge or want_conf)
    if llm_call:
        reply = chat([{"role": "system", "content": RULES + PASSAGES_TASK},
                      {"role": "user", "content": _passages_prompt(passages, judge, want_conf)
                       + _lang_line(lang)}],
                     max_tokens=MAX_TOKENS, role="review")
        data = _parse_json(reply)
        got = {}
        for item in data.get("passages") or []:
            try:
                n = int(item.get("n"))
            except (TypeError, ValueError):
                continue
            if n in judge:
                got[n] = _clean_judgement(item, passages[n - 1]["text"])
        for n in judge:  # a passage the model skipped counts as ok, but is not cached
            j = got.get(n, {"level": "ok", "reason": None, "unchecked": True})
            judged[n] = j
            if n in got:
                _remember(_passage_cache, hashes[n - 1], j)
        if want_conf:
            found_conf = _clean_conflicts(data.get("conflicts") or [], passages)
            _remember(_conflict_cache, conf_key, found_conf)
    return {"by_n": judged, "conflicts": found_conf, "llm_call": llm_call,
            "ms": round((time.perf_counter() - t0) * 1000)}


def check_draft(text: str, passages: list[dict], lang: str | None = None) -> dict:
    """Review an editor's draft: wording, factual errors, and the stance of each passage. One LLM call."""
    t0 = time.perf_counter()
    blocks = "\n\n".join(f"[{n}] (article: {p['title']})\n{p['text']}" for n, p in enumerate(passages, 1))
    lang_line = (f'\nWrite every "reason" and "problem" in {lang}. Keep "phrases", "draft_phrase", '
                 '"passage_phrase", "suggestion" and "fix" in their own language.') if lang else ""
    reply = chat([{"role": "system", "content": RULES + DRAFT_TASK + lang_line},
                  {"role": "user", "content": f"DRAFT:\n{text}\n\nPassages:\n\n{blocks}"}],
                 max_tokens=MAX_TOKENS, role="review")
    data = _parse_json(reply)
    draft = _clean_judgement(data.get("draft") or {}, text)
    evidence = {}
    for item in data.get("evidence") or []:
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(passages):
            continue
        stance = str(item.get("stance", "")).strip().lower()
        phrase = str(item.get("phrase") or "").strip()
        evidence[n] = {"stance": stance if stance in ("supports", "contradicts") else "unrelated",
                       "phrase": phrase if phrase and phrase in passages[n - 1]["text"] else None}
    facts = []
    for item in data.get("facts") or []:
        if not isinstance(item, dict):
            continue
        dp = str(item.get("draft_phrase") or "").strip()
        if not dp or dp not in text:
            print(f"[review] fact issue dropped (phrase not in draft): {item}", file=sys.stderr, flush=True)
            continue
        try:
            n = int(item.get("n"))
        except (TypeError, ValueError):
            n = None
        if n is not None and not 1 <= n <= len(passages):
            n = None
        pp = str(item.get("passage_phrase") or "").strip()
        facts.append({"draft_phrase": dp, "n": n,
                      "passage_phrase": pp if n and pp and pp in passages[n - 1]["text"] else None,
                      "problem": str(item.get("problem") or "").strip() or None,
                      "fix": str(item.get("fix") or "").strip() or None})
    return {"draft": draft, "facts": facts, "evidence": evidence,
            "ms": round((time.perf_counter() - t0) * 1000)}


def _lang_line(lang: str | None) -> str:
    if not lang:
        return ""
    return (f'\nWrite every "reason" and "about" in {lang}. Keep "phrases", '
            '"worst_sentence" and "suggestion" in the passage\'s own language.')


def _passages_prompt(passages, judge, want_conf) -> str:
    blocks = "\n\n".join(f"[{n}] (article: {p['title']})\n{p['text']}" for n, p in enumerate(passages, 1))
    return (f"Passages:\n\n{blocks}\n\nJudge: {', '.join(map(str, judge)) or 'none'}\n"
            f"Find conflicts: {'yes' if want_conf else 'no'}")


def _parse_json(reply: str) -> dict:
    m = re.search(r"\{.*\}", reply, re.S)  # tolerate ```json fences or a stray sentence
    if not m:
        raise ValueError(f"review model did not return JSON: {reply[:200]!r}")
    return json.loads(m.group(0))


def _clean_judgement(item: dict, text: str) -> dict:
    """Keep only what the text backs up: phrases must occur exactly in it."""
    if str(item.get("level", "")).lower() != "flagged":
        return {"level": "ok", "reason": None}
    phrases = [p.strip() for p in (item.get("phrases") or []) if isinstance(p, str) and p.strip()]
    phrases = [p for p in phrases if p in text][:3]
    worst = str(item.get("worst_sentence") or "").strip()
    category = str(item.get("category") or "").strip().lower()
    reason = str(item.get("reason") or "").strip()
    if category in CATEGORIES and not reason.lower().startswith(category):
        reason = f"{category}: {reason}" if reason else category
    return {"level": "flagged",
            "category": category if category in CATEGORIES else None,
            "reason": reason or None,
            "phrases": phrases,
            "worst_sentence": worst if worst and worst in text else None,
            "suggestion": str(item.get("suggestion") or "").strip() or None}


def _clean_conflicts(items: list, passages: list[dict]) -> list[dict]:
    """Only pairs from different articles whose phrases really occur in their passages."""
    out, seen = [], set()
    for c in items:
        try:
            a, b = c["a"], c["b"]
            na, nb = int(a["n"]), int(b["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (1 <= na <= len(passages) and 1 <= nb <= len(passages)) or na == nb:
            continue
        pa, pb = passages[na - 1], passages[nb - 1]
        fa, fb = str(a.get("phrase") or "").strip(), str(b.get("phrase") or "").strip()
        if pa["page_id"] == pb["page_id"] or not fa or not fb or fa not in pa["text"] or fb not in pb["text"]:
            print(f"[review] conflict dropped (same article or phrase not in passage): {c}",
                  file=sys.stderr, flush=True)
            continue
        key = tuple(sorted((na, nb)))
        if key in seen:
            continue
        seen.add(key)
        out.append({"about": str(c.get("about") or "").strip() or None,
                    "a": _side(na, pa, fa), "b": _side(nb, pb, fb)})
    return out


def _side(n, p, phrase):
    return {"n": n, "title": p["title"], "page_id": p["page_id"], "rev_id": p["rev_id"],
            "url": p["url"], "phrase": phrase}
