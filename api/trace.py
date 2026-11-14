"""POST /api/trace: where a phrase in an article came from, from the article's stored edit history
(article_version, loaded by pacome/rag-demo/history.py or eval/load_history.py; Finnish articles only).

  1. Every version of the article, oldest first (FOR SYSTEM_TIME ALL). Each version's wikitext
     is matched with links unwrapped ([[target|label]] -> label, [[x]] -> x), bold/italic marks
     dropped and HTML entities decoded, because the passages the phrase comes from are plain text.
  2. first: the first version that contains the phrase.
  3. introduced: the edit that gave the sentence this meaning. The "claim" is the phrase without
     its dates and numbers (or, for a phrase that is only a date, the words just before it in the
     first version); walking back from the first version, the earliest version of the unbroken
     run that already has the claim. E.g. "Suomen senaatti antoi itsenäisyysjulistuksen 6.12.1917"
     (rev 1975299) before "... 6. joulukuuta 1917" (rev 1975440).
  4. runs: the stretches of consecutive versions that contain the phrase, with the edit that
     ended each (removed_by) unless it lasts to the current version; and the total count.
Editor names and IP addresses are never returned: VERSIONS_SQL does not select the editor column,
only whether the edit was anonymous, and edit comments are redacted (see redact(): user links,
every editor name in the loaded history, IP addresses).

GET /api/trace/available?page_ids=1,2: which of the pages have history, for the page's Trace links.
"""
import html
import re
import threading
import time
from collections import OrderedDict

from .search import connect

MAX_PHRASE = 500
CACHE_PAGES = 8
REV_URL = "https://fi.wikipedia.org/w/index.php?oldid={rev_id}"

VERSIONS_SQL = """
SELECT rev_id, title, row_start, anonymous, minor, comment, text
FROM article_version FOR SYSTEM_TIME ALL
WHERE page_id = %s
ORDER BY row_start"""

STAMP_SQL = """
SELECT COUNT(*), MAX(row_start) FROM article_version FOR SYSTEM_TIME ALL WHERE page_id = %s"""

AVAILABLE_SQL = """
SELECT DISTINCT page_id FROM article_version FOR SYSTEM_TIME ALL WHERE page_id IN ({ph})"""

LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
EXT_LINK = re.compile(r"\[https?://\S+\s+([^\]]*)\]")
MONTHS = ("tammikuuta helmikuuta maaliskuuta huhtikuuta toukokuuta kesäkuuta heinäkuuta elokuuta "
          "syyskuuta lokakuuta marraskuuta joulukuuta").split()
DATE_WORD = re.compile(r"^[\d.,:–\-/]+$")

_lock = threading.Lock()
_pages: "OrderedDict[int, tuple]" = OrderedDict()   # page_id -> (stamp, versions)


def plain(wikitext: str) -> str:
    """Wikitext as the plain passages read: links unwrapped, '' and ''' dropped, entities decoded."""
    t = LINK.sub(r"\1", wikitext or "")
    t = EXT_LINK.sub(r"\1", t)
    t = html.unescape(t.replace("'''", "").replace("''", ""))
    return " ".join(t.split())


def available(page_ids: list[int]) -> list[int]:
    ids = sorted({int(p) for p in page_ids})[:100]
    if not ids:
        return []
    with connect() as con, con.cursor() as cur:
        cur.execute(AVAILABLE_SQL.format(ph=",".join(["%s"] * len(ids))), ids)
        return sorted(r[0] for r in cur.fetchall())


def _versions(page_id: int) -> list[dict]:
    """All versions with their plain text, cached until the article's history changes."""
    with connect() as con, con.cursor() as cur:
        cur.execute(STAMP_SQL, (page_id,))
        stamp = tuple(cur.fetchone())
        if not stamp[0]:
            return []
        with _lock:
            hit = _pages.get(page_id)
        if hit and hit[0] == stamp:
            return hit[1]
        names = _names_re(cur)
        cur.execute(VERSIONS_SQL, (page_id,))
        rows = [{"rev_id": r[0], "title": r[1], "date": r[2], "anonymous": bool(r[3]),
                 "minor": bool(r[4]), "comment": redact(r[5], names), "text": plain(r[6])}
                for r in cur.fetchall()]
    with _lock:
        _pages[page_id] = (stamp, rows)
        _pages.move_to_end(page_id)
        while len(_pages) > CACHE_PAGES:
            _pages.popitem(last=False)
    return rows


# Edit comments name people: revert summaries link the user ("[[Special:Contributions/1.2.3.4|...]]",
# "[[Toiminnot:Contributions/..]]", "[[:User:Name|Name]]", "([[Keskustelu_käyttäjästä:Name|keskustelu]])"),
# and name them in plain text ("by Name using popups", "käyttäjältä Name"). So, in this order:
#   1. user-talk links (with their parentheses) go, user / contributions links become [user];
#   2. every editor name in the loaded history (all articles) becomes [user]: the names are read
#      only for this, and never returned;
#   3. IP addresses become [IP] (IPv6 needs 5+ groups, so a time like 12:30:45 stays).
USER_WORDS = r"(?:user|käyttäjä|användare|contributions|muokkaukset|bidrag)"
TALK_LINK = re.compile(r"\s*\(?\[\[[^\]]*?(?:talk|keskustelu|diskussion)[ _]?(?:käyttäjästä)?[^\]]*?:[^\]]*\]\]\)?", re.I)
USER_LINK = re.compile(r"\[\[[^\]|]*?" + USER_WORDS + r"[^\]|]*?[:/][^\]]*\]\]", re.I)
USER_LINK_CUT = re.compile(r"\[\[[^\]]*?" + USER_WORDS + r"[^\]]*$", re.I)   # comment truncated mid-link
IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}(?![\w:])")
NAMES_SQL = "SELECT DISTINCT `user` FROM article_version FOR SYSTEM_TIME ALL WHERE `user` IS NOT NULL"
_names: dict = {"stamp": None, "re": None}


def _names_re(cur):
    """One regex of every editor name in article_version, rebuilt when the history grows."""
    cur.execute("SELECT COUNT(*) FROM article_version FOR SYSTEM_TIME ALL")
    stamp = cur.fetchone()[0]
    if _names["stamp"] != stamp:
        cur.execute(NAMES_SQL)
        names = sorted({r[0].strip() for r in cur.fetchall() if r[0] and len(r[0].strip()) >= 2}, key=len, reverse=True)
        alts = [re.escape(n) + "|" + re.escape(n.replace(" ", "_")) if " " in n else re.escape(n) for n in names]
        _names.update(stamp=stamp, re=re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)") if alts else None)
    return _names["re"]


def redact(comment: str, names=None) -> str:
    """An edit comment without user names or IP addresses; other wiki links show their label."""
    c = TALK_LINK.sub("", html.unescape(comment or ""))
    c = USER_LINK.sub("[user]", c)
    c = USER_LINK_CUT.sub("[user]…", c)
    c = IPV6.sub("[IP]", IPV4.sub("[IP]", c))
    if names is not None:
        c = names.sub("[user]", c)
    c = LINK.sub(r"\1", c)
    return " ".join(c.split())


def _rev(v: dict) -> dict:
    return {"rev_id": v["rev_id"], "date": v["date"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "comment": v["comment"] or None,   # already redacted in _versions
            "anonymous": v["anonymous"], "minor": v["minor"],
            "url": REV_URL.format(rev_id=v["rev_id"])}


def _is_date_word(w: str) -> bool:
    return bool(DATE_WORD.match(w)) or w.lower().strip(".,") in MONTHS


def _claim(phrase: str, first_text: str) -> str:
    """The phrase without its dates: the longest run of non-date words; when fewer than 2 words
    are left, the (up to) 4 words before the phrase in the first version plus what is left."""
    words, runs, cur = phrase.split(), [], []
    for w in words:
        if _is_date_word(w):
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        runs.append(cur)
    best = max(runs, key=len) if runs else []
    if len(best) >= 2:
        return " ".join(best)
    k = first_text.find(phrase)
    before = [w for w in first_text[:k].split()[-4:] if not w.startswith(("=", "{", "|"))]
    return " ".join(before + best)


def _snippet(text: str, needle: str, width: int = 140) -> str | None:
    k = text.find(needle)
    if k < 0:
        return None
    a, b = max(0, k - width), min(len(text), k + len(needle) + width)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def trace(page_id: int, phrase: str) -> dict:
    t0 = time.perf_counter()
    phrase = " ".join(html.unescape(phrase or "").split())
    if not phrase:
        raise ValueError("phrase is empty")
    if len(phrase) > MAX_PHRASE:
        raise ValueError(f"phrase is longer than {MAX_PHRASE} characters")
    versions = _versions(int(page_id))
    if not versions:
        return {"available": False, "page_id": page_id}

    has = [phrase in v["text"] for v in versions]
    out = {"available": True, "page_id": page_id, "title": versions[-1]["title"], "phrase": phrase,
           "matched_against": "wikitext with links unwrapped, bold/italic marks dropped, entities decoded",
           "versions_total": len(versions), "versions_with_phrase": sum(has),
           "history_from": _rev(versions[0])["date"], "history_to": _rev(versions[-1])["date"],
           "in_current_version": has[-1], "first": None, "introduced": None, "runs": [],
           "sql": VERSIONS_SQL.strip()}
    if not any(has):
        out["took_ms"] = round((time.perf_counter() - t0) * 1000)
        return out

    first = has.index(True)
    out["first"] = {**_rev(versions[first]), "text": _snippet(versions[first]["text"], phrase)}

    claim = _claim(phrase, versions[first]["text"])
    origin = first
    while claim and origin > 0 and claim in versions[origin - 1]["text"]:
        origin -= 1
    out["introduced"] = {**_rev(versions[origin]), "claim": claim, "same_as_first": origin == first,
                         "text": _snippet(versions[origin]["text"], claim),
                         "before": _rev(versions[origin - 1]) if origin > 0 else None}

    i = 0
    while i < len(has):
        if not has[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(has) and has[j + 1]:
            j += 1
        out["runs"].append({"start": _rev(versions[i]), "end": _rev(versions[j]), "versions": j - i + 1,
                            "removed_by": _rev(versions[j + 1]) if j + 1 < len(versions) else None})
        i = j + 1
    out["took_ms"] = round((time.perf_counter() - t0) * 1000)
    return out
