"""Editor mode of /api/ask: an "evidence" block for each side of a conflict and each flagged
passage. It collects pointers an editor can check; it NEVER says which side is correct.

    {"label": "Evidence for the editor to check",
     "phrase": the phrase this is about,
     "trace_available": true when the phrase was found in the article's current wikitext,
     "trace_note": why not (no history loaded, phrase not in the current wikitext, ...),
     "has_reference": true/false when traced, else null,
     "reference_text": the <ref> content attached to that sentence (several joined by newlines),
                       else null,
     "wikitext_rev_id": the revision the trace was made in, else null,
     "traced_sentence": (when traced) the sentence as it reads in that wikitext, cleaned,
     "agreeing": {"count", "values", "passages": [{n, title, sentence}], "note"} or null when the
                 phrase has no number or date to compare,
     "other_languages": {"sv"|"en"|"fi": null (no such version) or
                         {title, url, sentence or null, similarity, values, same_values}}}

How each part is found:
  trace      only articles whose history is loaded (article_version with wikitext; the current row,
             i.e. today's revision, which can be newer than the passage). The wikitext is cut into
             paragraphs, each cleaned with the same cleaner as as_of (api/ask.py clean_wikitext);
             the paragraph whose clean text contains the phrase holds it. The phrase's sentence
             is bounded in that clean text, and a <ref> counts as attached when it stands inside
             the sentence or right after its final punctuation. <ref name=x/> is resolved to the
             named ref's content. Only <ref> tags count ({{sfn}}-style templates do not).
  agreeing   the numbers in the phrase (for a conflict: those the other side does not have),
             looked up as whole values in the other retrieved passages of OTHER articles; a
             passage counts when it has all of them and none of the other side's. Same value
             only: it is not checked that the passage is about the same thing.
  other      the sv / en (or fi) article with the same wikidata_id: its sentences that share a
  languages  number or a name with the phrase's sentence are embedded with bge-m3 (one Ollama
             call per request) and the most similar one above MIN_SIMILARITY is returned.
"""
import json
import re
import sys

import mwparserfromhell

from .search import connect, embed_texts

LABEL = "Evidence for the editor to check"
NOTE = ("Pointers for checking the sources, not a judgement of which text is correct.")
MIN_SIMILARITY = 0.6        # bge-m3 cosine, sentence vs sentence across languages
MAX_SENTENCES = 40          # candidate sentences per other-language article and phrase
MIN_SIMILARITY_SHARED = 0.45  # ... enough when the sentence shares an exact figure (>= 100) with it
LANGS = ("fi", "sv", "en")

# Numbers. Not digits that belong to a name or code ("TB-3", "A4"). Thousands are grouped with a
# space in fi / sv ("26 662") and a comma in en ("26,662"); the decimal mark is the other one.
_SPACES = "[ \u00a0\u202f\u2009]"
_NOT_IN_WORD = r"(?<![A-Za-z\u00c0-\u024f\d])(?<![A-Za-z\u00c0-\u024f]-)"
_NUMBER = {
    "fi": re.compile(_NOT_IN_WORD + r"(?:\d{1,3}(?:" + _SPACES + r"\d{3})+(?!\d)|\d+(?:,\d+)?)"),
    "en": re.compile(_NOT_IN_WORD + r"(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+(?:\.\d+)?)"),
}
# A decade or century is its own value, not a year: 1940-luvulla / 1940-talet / 1940s -> "1940s".
_DECADE = re.compile(r"(?<!\d)(\d{2,3}0)(?:-?luv\w*|-?luk\w*|-?tal(?:et|ets)?\b|'?s\b)", re.I)
_DOT_DATE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{3,4})(?!\d)")   # 30.11.1939 -> 30, 1939
_SENTENCE_END = re.compile(r"[.!?…](?=\s+[\"'«»(\[A-ZÅÄÖ0-9]|\s*$)")
_NAME = re.compile(r"\b[A-ZÅÄÖ][\wåäöÅÄÖ-]{3,}")
# An opening <ref ...> must not end in "/" (that is a self-closing <ref name=x/>, no body).
_REF = re.compile(r"<ref\b[^>]*?/\s*>|<ref\b[^>]*?(?<!/)>.*?</ref\s*>", re.S | re.I)
_REF_NAME = re.compile(r"""name\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s/>]+))""", re.I)
_REF_BODY = re.compile(r"<ref\b([^>]*?)(?<!/)>(.*?)</ref\s*>", re.S | re.I)
_SPLIT_SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[\"'«(\[A-ZÅÄÖ])")

WIKITEXT_SQL = """
SELECT page_id, rev_id, text FROM article_version WHERE page_id IN ({ph}) AND text <> ''"""
HISTORY_SQL = "SELECT DISTINCT page_id FROM article_version FOR SYSTEM_TIME ALL WHERE page_id IN ({ph})"
WIKIDATA_SQL = "SELECT lang, page_id, wikidata_id FROM article WHERE (lang, page_id) IN ({ph})"
VERSIONS_SQL = """
SELECT lang, page_id, wikidata_id, title, url, text FROM article
WHERE wikidata_id IN ({ph}) AND lang IN ('fi', 'sv', 'en')"""


def _norm(s: str) -> str:
    s = (s or "").replace(" ", " ").replace("–", "-").replace("—", "-")
    s = re.sub(r"[“”„″]", '"', s)
    s = re.sub(r"[‘’‚′]", "'", s)
    return " ".join(s.split())


def values(text: str, lang: str = "fi") -> set[str]:
    """Numbers in a text, normalised so they compare across languages: "26 662" (fi, sv) and
    "26,662" (en) -> "26662", "2,5" (fi) and "2.5" (en) -> "2.5", "1940-luvulla" -> "1940s",
    "30.11.1939" -> "30", "1939" (as "30. marraskuuta 1939" gives)."""
    text, out = text or "", set()
    for m in _DECADE.finditer(text):
        out.add(m.group(1) + "s")
    text = _DECADE.sub(" ", text)
    for d, _month, y in _DOT_DATE.findall(text):
        out |= {str(int(d)), str(int(y))}
    text = _DOT_DATE.sub(" ", text)
    en = lang == "en"
    for m in _NUMBER["en" if en else "fi"].findall(text):
        m = re.sub(_SPACES, "", m)
        m = m.replace(",", "") if en else m.replace(",", ".")
        out.add(m if "." in m else str(int(m)))
    return out


def sentences(text: str) -> list[str]:
    """Sentences, never across a line break (dataset S keeps one paragraph per line)."""
    return [s for line in (text or "").split("\n") for s in _SPLIT_SENTENCES.split(line.strip()) if s]


def sentence_around(text: str, phrase: str) -> tuple[int, int] | None:
    """(start, end) of the sentence of `text` that contains `phrase`, or None."""
    i = text.find(phrase)
    if i < 0:
        return None
    start = 0
    for m in _SENTENCE_END.finditer(text, 0, i):
        start = m.end()
    while start < len(text) and text[start] == " ":
        start += 1
    # From the phrase's last word, so a phrase that ends in "." ends its own sentence.
    m = _SENTENCE_END.search(text, i + len(phrase.rstrip(".!?… ")))
    return start, (m.end() if m else len(text))


def _sentence(text: str, phrase: str) -> str:
    b = sentence_around(text, phrase)
    return text[b[0]:b[1]].strip() if b else phrase


def _clean(wikitext: str) -> str:
    from .ask import clean_wikitext  # here, so importing this module does not import ask
    try:
        return _norm(clean_wikitext(wikitext))
    except Exception:
        return _norm(mwparserfromhell.parse(wikitext).strip_code())


# --- trace in the current wikitext ---

def _named_refs(wikitext: str) -> dict[str, str]:
    out = {}
    for attrs, body in _REF_BODY.findall(wikitext):
        m = _REF_NAME.search(attrs)
        if m and body.strip():
            out.setdefault(next(g for g in m.groups() if g), body.strip())
    return out


def _ref_content(tag: str, named: dict[str, str]) -> str | None:
    m = _REF_BODY.match(tag)
    if m and m.group(2).strip():
        return m.group(2).strip()
    n = _REF_NAME.search(tag)
    return named.get(next(g for g in n.groups() if g)) if n else None


def _find_phrase(clean: str, phrase: str) -> tuple[str, str] | None:
    """The phrase as it occurs in the clean text: exactly, or else its longest clause."""
    p = _norm(phrase)
    if p and p in clean:
        return p, "exact"
    parts = sorted((x.strip() for x in re.split(r"[,;:()\"]", p)), key=len, reverse=True)
    for part in parts:
        if len(part) >= 15 and part in clean:
            return part, "part of the phrase"
    return None


def trace(wikitext: str, rev_id: int, phrase: str) -> dict:
    named = _named_refs(wikitext)
    # Cleaning is the slow part: only clean blocks that contain a number or a longer word of it.
    # Raw digit runs ("85&nbsp;555" in wikitext still contains "555"), not normalised values().
    probes = set(re.findall(r"\d{2,}", phrase)) | {w.strip(".,;:()\"'") for w in _norm(phrase).split() if len(w) > 6}
    for block in re.split(r"\n\s*\n", wikitext):
        if probes and not any(p in block for p in probes):
            continue
        clean = _clean(block)
        found = _find_phrase(clean, phrase)
        if not found:
            continue
        piece, how = found
        start, end = sentence_around(clean, piece)
        refs = []
        for m in _REF.finditer(block):
            pos = len(_clean(block[:m.start()]))
            if start <= pos <= end:
                content = _ref_content(m.group(0), named)
                refs.append(content if content else m.group(0))
        refs = list(dict.fromkeys(refs))
        return {"trace_available": True,
                "trace_note": None if how == "exact" else f"matched {how}: {piece!r}",
                "has_reference": bool(refs), "reference_text": "\n".join(refs) or None,
                "wikitext_rev_id": rev_id, "traced_sentence": clean[start:end].strip()}
    return {"trace_available": False,
            "trace_note": "phrase not found in the current wikitext (the passage may be from an "
                          "older text, or the wording comes from a template)",
            "has_reference": None, "reference_text": None, "wikitext_rev_id": rev_id}


NO_HISTORY = {"trace_available": False, "trace_note": "history (wikitext) is not loaded for this article",
              "has_reference": None, "reference_text": None, "wikitext_rev_id": None}


# --- agreeing passages ---

def agreeing(phrase: str, page_id: int, passages: list[dict], against: str | None = None,
             lang: str = "fi", against_lang: str = "fi") -> dict | None:
    """Other retrieved passages from other articles that state the same number(s)."""
    mine = values(phrase, lang)
    other = values(against, against_lang) if against else set()
    if against:
        mine, other = mine - other, other - mine
    if not mine:
        return None
    found = []
    for n, p in enumerate(passages, 1):
        if p["page_id"] == page_id or (against and _norm(against) in _norm(p["text"])):
            continue
        lang = p.get("lang", "fi")
        have = values(p["text"], lang)
        if mine <= have and not (other & have):
            v = next(iter(mine))
            sent = next((s for s in sentences(p["text"]) if v in values(s, lang)), p["text"][:300])
            found.append({"n": n, "title": p["title"], "page_id": p["page_id"], "sentence": sent})
    return {"count": len({f["page_id"] for f in found}), "values": sorted(mine), "passages": found,
            "note": "same value in another article; not checked that it is about the same thing"}


# --- other languages ---

def _candidates(text: str, lang: str, point: str, point_lang: str, context: str = "") -> list[str]:
    """Sentences of `text` (in `lang`) sharing a number or a name (first 5 letters, so Finnish
    case endings still match) with the point; numbers and names of the whole passage (`context`)
    count too, with less weight, for points that have none of their own."""
    want_v = values(point, point_lang)
    want_n = {w[:5].lower() for w in _NAME.findall(point)}
    ctx_v = values(context, point_lang) - want_v
    ctx_n = {w[:5].lower() for w in _NAME.findall(context)} - want_n
    kinds = {_kind(v) for v in want_v} - {None}
    scored = []
    for s in sentences(text):
        s = " ".join(s.split())
        if len(s) < 25 or len(s) > 600 or s[0] in "\"'“”«" or _BIBLIOGRAPHY.search(s):
            continue
        sv, sn = values(s, lang), {w[:5].lower() for w in _NAME.findall(s)}
        # The same kind of number counts too: a disagreeing text has a DIFFERENT value
        # ("26 662" vs "25,904"), which an exact match would never find.
        same_kind = bool(kinds & {_kind(v) for v in sv})
        score = (4 * len(want_v & sv) + 2 * len(want_n & sn) + 2 * same_kind
                 + len(ctx_v & sv) + len(ctx_n & sn))
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:MAX_SENTENCES]]


def _exact_shared(point_values: set[str], s: str, lang: str) -> int:
    return len(point_values & values(s, lang))


# Lines of a bibliography or reference list: a translated title "[...]", "(in Finnish)", ISBN, pages.
_BIBLIOGRAPHY = re.compile(r"\[[^\]]{8,}\]|\((?:in|på) [A-ZÅÄÖ]?[a-zåäö]+\)|\bISBN\b|\bISSN\b|"
                           r"\bpp?\. ?\d|\bs\. ?\d+[–-]|\bdoi:", re.I)


def _kind(v: str) -> str | None:
    """What kind of number: a year in a 30-year window ("y1930"), or an n-digit amount ("n5").
    Small numbers (days, counts under 100) are too common to say anything."""
    if not v.isdigit():
        return None
    n = int(v)
    if 1000 <= n <= 2100:
        return f"y{n // 30}"
    return f"n{len(v)}" if n >= 100 else None


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# --- all together ---

def add_evidence(items: list[dict], passages: list[dict]) -> None:
    """items: [{"target": dict to get the "evidence" key, "page_id", "lang", "phrase",
    "sentence" (the phrase's sentence in the passage), "against" (the other side's phrase or
    None)}]. Fills target["evidence"] in place. Never raises: on failure each block says so."""
    if not items:
        return
    try:
        _fill(items, passages)
    except Exception as e:
        print(f"[evidence] failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        for it in items:
            it["target"]["evidence"] = {"label": LABEL, "error": f"{type(e).__name__}: {e}"}


def _fill(items: list[dict], passages: list[dict]) -> None:
    fi_ids = sorted({it["page_id"] for it in items if it["lang"] == "fi"})
    keys = sorted({(it["lang"], it["page_id"]) for it in items})
    wikitexts, with_history, wikidata, versions = {}, set(), {}, {}
    with connect() as con, con.cursor() as cur:
        if fi_ids:
            ph = ",".join(["%s"] * len(fi_ids))
            cur.execute(WIKITEXT_SQL.format(ph=ph), fi_ids)
            wikitexts = {pid: (rev, wt) for pid, rev, wt in cur.fetchall()}
            cur.execute(HISTORY_SQL.format(ph=ph), fi_ids)
            with_history = {r[0] for r in cur.fetchall()}
        cur.execute(WIKIDATA_SQL.format(ph=",".join(["(%s,%s)"] * len(keys))), [v for k in keys for v in k])
        wikidata = {(lang, pid): wd for lang, pid, wd in cur.fetchall() if wd}
        wds = sorted(set(wikidata.values()))
        if wds:
            cur.execute(VERSIONS_SQL.format(ph=",".join(["%s"] * len(wds))), wds)
            for lang, pid, wd, title, url, text in cur.fetchall():
                versions.setdefault(wd, {})[lang] = {"page_id": pid, "title": title, "url": url, "text": text}

    # Other languages: collect every candidate sentence first, then one embedding call.
    jobs, texts = [], []
    for i, it in enumerate(items):
        wd = wikidata.get((it["lang"], it["page_id"]))
        for lang in LANGS:
            if lang == it["lang"]:
                continue
            v = (versions.get(wd) or {}).get(lang) if wd else None
            if not v:
                continue
            cands = _candidates(v["text"], lang, it["sentence"], it["lang"], it.get("context", ""))
            jobs.append((i, lang, v, cands, len(texts)))
            texts += [it["sentence"]] + cands
    vectors = embed_texts(texts) if texts else []

    other: dict[int, dict] = {i: {lang: None for lang in LANGS if lang != it["lang"]}
                              for i, it in enumerate(items)}
    for i, lang, v, cands, at in jobs:
        # First choice: a sentence sharing an exact figure (100 or more) with the point's sentence,
        # if it is at least loosely similar: the same figure is strong evidence of the same point.
        # Otherwise the most similar sentence, if similar enough.
        sent_values = {v for v in values(items[i]["sentence"], items[i]["lang"]) if _kind(v)}
        sims = [(_cosine(vectors[at], vectors[at + 1 + k]), s) for k, s in enumerate(cands)]
        shared = [(sim, s) for sim, s in sims
                  if sim >= MIN_SIMILARITY_SHARED and _exact_shared(sent_values, s, lang)]
        best_any = max(sims, default=(None, None))
        if shared:
            score, hit = max(shared)
            how = "shares a figure with the passage"
        elif best_any[0] is not None and best_any[0] >= MIN_SIMILARITY:
            score, hit = best_any
            how = "most similar sentence"
        else:
            score, hit, how = best_any[0], None, None
        point_values = values(items[i]["phrase"], items[i]["lang"])
        hit_values = values(hit, lang) if hit else set()
        other[i][lang] = {"title": v["title"], "url": v["url"], "page_id": v["page_id"],
                          "sentence": hit,
                          "similarity": round(score, 3) if score is not None else None,
                          "matched_by": how,
                          "values": sorted(hit_values),
                          "same_values": (point_values <= hit_values) if hit and point_values else None,
                          "note": None if hit else "no matching passage found"}

    for i, it in enumerate(items):
        pid = it["page_id"]
        if it["lang"] == "fi" and pid in wikitexts:
            rev, wt = wikitexts[pid]
            t = trace(wt, rev, it["phrase"])
        elif it["lang"] == "fi" and pid in with_history:
            t = {**NO_HISTORY, "trace_note": "history is loaded, but not the wikitext of this article"}
        else:
            t = NO_HISTORY
        it["target"]["evidence"] = {
            "label": LABEL, "note": NOTE, "phrase": it["phrase"], **t,
            "agreeing": agreeing(it["phrase"], pid, passages, it.get("against"),
                                 it["lang"], it.get("against_lang", "fi")),
            "other_languages": other[i]}


def items_for_ask(conflicts: list[dict] | None, flagged: list[dict], passages: list[dict]) -> list[dict]:
    """Evidence items for /api/ask editor mode: both sides of each conflict, and each flagged
    passage (its worst sentence, else its first flagged phrase)."""
    items = []
    for c in conflicts or []:
        for side, other_side in (("a", "b"), ("b", "a")):
            s = c[side]
            p = passages[s["n"] - 1]
            items.append({"target": s, "page_id": s["page_id"], "lang": p.get("lang", "fi"),
                          "phrase": s["phrase"], "sentence": _sentence(p["text"], s["phrase"]),
                          "context": p["text"], "against": c[other_side]["phrase"],
                          "against_lang": passages[c[other_side]["n"] - 1].get("lang", "fi")})
    for f in flagged:
        j = f.get("neutrality") or {}
        phrase = j.get("worst_sentence") or (j.get("phrases") or [None])[0]
        if not phrase:
            f["evidence"] = {"label": LABEL, "note": NOTE, "phrase": None, **NO_HISTORY,
                             "trace_note": "the review gave no exact phrase to trace",
                             "agreeing": None, "other_languages": None}
            continue
        p = passages[f["n"] - 1]
        items.append({"target": f, "page_id": f["page_id"], "lang": p.get("lang", "fi"),
                      "phrase": phrase, "sentence": _sentence(p["text"], phrase), "context": p["text"],
                      "against": None})
    return items


if __name__ == "__main__":   # quick manual check: python -m api.evidence <page_id> "<phrase>"
    from dotenv import load_dotenv
    load_dotenv()
    pid, phrase = int(sys.argv[1]), sys.argv[2]
    with connect() as con, con.cursor() as cur:
        cur.execute(WIKITEXT_SQL.format(ph="%s"), (pid,))
        row = cur.fetchone()
    print(json.dumps(trace(row[2], row[1], phrase) if row else NO_HISTORY, ensure_ascii=False, indent=2))
