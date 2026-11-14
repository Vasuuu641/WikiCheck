"""POST /api/check: an editor's draft sentence, checked before it goes into Wikipedia.

  1. The same retrieval as /api/ask (the draft is the "question"): 8 passages, from at least 3
     articles when there are.
  2. ONE call to the review model (api/review.py check_draft): the neutrality review of the draft
     itself, with the same rules as for passages; the draft's factual errors against the passages;
     and for each passage whether it supports or contradicts the draft (with the exact phrase).
  3. A check in code, whatever the model said: every number in the draft must occur as a whole
     number in at least one passage ("808" is not found in "1808").
Returns {text, neutrality, fact_issues, supporting, contradicting, sql, took_ms}; supporting /
contradicting are citations in the /api/ask shape plus `phrase` and `edit_url`. A passage named in
a fact issue counts as contradicting. Each fact issue:
  {kind: "model" | "number_not_found", draft_phrase, problem, fix, source: citation | null}
"""
import hashlib
import re
import sys
import time
from collections import OrderedDict

from .ask import _Timer, edit_url, retrieve
from .review import check_draft, review_model

MAX_CHARS = 2000
CHECK_PASSAGES = 8
CHECK_MIN_ARTICLES = 3
_cache: "OrderedDict[str, dict]" = OrderedDict()   # draft + passages -> check_draft result
# a whole number, with an optional ordinal ending (21st, 21:a); "808" is not found in "1808"
NUMBER = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)*)(?:st|nd|rd|th|:a|:e)?(?!\w)")
LANGUAGES = {"en": "English", "fi": "Finnish", "sv": "Swedish"}


def _numbers(text: str) -> set[str]:
    """Numbers of two or more digits (single digits are too common to check on their own)."""
    return {n for n in NUMBER.findall(text) if len(n.replace(".", "").replace(",", "")) >= 2}


def check(text: str, trace: dict | None = None, lang: str | None = None) -> dict:
    t0 = time.perf_counter()
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("text is empty")
    if len(text) > MAX_CHARS:
        raise ValueError(f"text is longer than {MAX_CHARS} characters")
    if lang is not None and lang not in LANGUAGES:
        raise ValueError(f"lang must be one of {', '.join(LANGUAGES)}")

    timer, tr = _Timer(), {"review_model": review_model()}
    passages, sql, message = retrieve(text, None, timer, tr, n_passages=CHECK_PASSAGES,
                                      min_articles=CHECK_MIN_ARTICLES)
    if message:
        return {"text": text, "neutrality": None, "fact_issues": [], "supporting": [], "contradicting": [],
                "sql": sql, "took_ms": round((time.perf_counter() - t0) * 1000)}

    key = hashlib.sha1("\x00".join([lang or "", text] + [p["text"] for p in passages]).encode()).hexdigest()
    with timer("review"):
        result = _cache.get(key)
        if result is None:
            result = check_draft(text, passages, LANGUAGES.get(lang))
            _cache[key] = result
            while len(_cache) > 500:
                _cache.popitem(last=False)
    def cite(n, phrase):
        p = passages[n - 1]
        return {"n": n, "page_id": p["page_id"], "rev_id": p["rev_id"], "title": p["title"],
                "url": p["url"], "passage": p["text"], "phrase": phrase, "edit_url": edit_url(p)}

    issues = [{"kind": "model", "draft_phrase": f["draft_phrase"], "problem": f["problem"],
               "fix": f["fix"], "source": cite(f["n"], f["passage_phrase"]) if f["n"] else None}
              for f in result.get("facts", [])]
    in_passages = set().union(*(_numbers(p["text"]) for p in passages))
    for num in sorted(_numbers(text) - in_passages, key=text.find):
        if not any(num in _numbers(i["draft_phrase"]) for i in issues):
            issues.append({"kind": "number_not_found", "draft_phrase": num, "problem": None,
                           "fix": None, "source": None})
    wrong = {i["source"]["n"] for i in issues if i["source"]}

    out = {"text": text, "neutrality": result["draft"], "fact_issues": issues,
           "supporting": [], "contradicting": []}
    for n, p in enumerate(passages, 1):
        ev = result["evidence"].get(n)
        stance = "contradicts" if n in wrong else (ev or {}).get("stance")
        if stance in ("supports", "contradicts"):
            if n in wrong:
                phrase = next(i["source"]["phrase"] for i in issues if i["source"] and i["source"]["n"] == n)
            else:
                phrase = ev["phrase"]
            out["supporting" if stance == "supports" else "contradicting"].append(cite(n, phrase))
    out["sql"] = sql
    out["took_ms"] = timer.ms["total"] = round((time.perf_counter() - t0) * 1000)
    print(f"[check] {text[:60]!r} timings ms: {timer.ms}", file=sys.stderr, flush=True)
    if trace is not None:
        tr["timings_ms"] = timer.ms
        tr["passages"] = [{"n": n, "title": p["title"], "score": round(p["score"], 4),
                           "stance": "contradicts" if n in wrong else
                                     (result["evidence"].get(n) or {}).get("stance", "unrelated")}
                          for n, p in enumerate(passages, 1)]
        trace.update(tr)
    return out
