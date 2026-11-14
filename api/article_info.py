"""GET /api/article/{page_id}/info: what the page's info box shows about one article, from the DB.

    title, page_id, lang, rev_id, date_modified, wikidata_id, text_length
    languages   every article row with the same wikidata_id: [{lang, page_id, title, url}]
    categories  up to 8, from article_category
    history     only if article_state has versions of this page (Finnish only), else null:
                {versions, first_edit, latest_edit, reverts}
                A revert is a version whose text (sha1) equals an earlier version that is not
                the one right before it: the page went back to how it was.
"""
from datetime import datetime

from .search import connect

LANGS = ("fi", "sv", "en")
MAX_CATEGORIES = 8

ARTICLE_SQL = """
SELECT lang, page_id, rev_id, title, url, wikidata_id, date_modified, CHAR_LENGTH(text)
FROM article WHERE lang = %s AND page_id = %s"""

LANGUAGES_SQL = """
SELECT lang, page_id, title, url FROM article WHERE wikidata_id = %s
ORDER BY FIELD(lang, 'fi', 'sv', 'en')"""

CATEGORIES_SQL = """
SELECT category FROM article_category WHERE lang = %s AND page_id = %s ORDER BY category LIMIT %s"""

# Only sha1 and the start time of each version: never the wikitext (Talvisota has 2,143 versions).
HISTORY_SQL = """
SELECT sha1, row_start FROM article_state FOR SYSTEM_TIME ALL
WHERE page_id = %s ORDER BY row_start"""


def info(page_id: int, lang: str = "fi") -> dict | None:
    if lang not in LANGS:
        raise ValueError(f"lang must be one of {', '.join(LANGS)}")
    with connect() as con, con.cursor() as cur:
        cur.execute(ARTICLE_SQL, (lang, page_id))
        row = cur.fetchone()
        if not row:
            return None
        lang, page_id, rev_id, title, url, wikidata_id, modified, length = row
        languages = [{"lang": lang, "page_id": page_id, "title": title, "url": url}]
        if wikidata_id:
            cur.execute(LANGUAGES_SQL, (wikidata_id,))
            languages = [dict(zip(("lang", "page_id", "title", "url"), r)) for r in cur.fetchall()]
        cur.execute(CATEGORIES_SQL, (lang, page_id, MAX_CATEGORIES))
        categories = [c for (c,) in cur.fetchall()]
        history = None
        if lang == "fi":  # article_state holds Finnish pages only (eval/load_history.py)
            cur.execute("SET time_zone = '+00:00'")
            cur.execute(HISTORY_SQL, (page_id,))
            versions = cur.fetchall()
            if versions:
                history = {"versions": len(versions), "first_edit": _iso(versions[0][1]),
                           "latest_edit": _iso(versions[-1][1]), "reverts": _reverts([s for s, _ in versions])}
    return {"title": title, "page_id": page_id, "lang": lang, "rev_id": rev_id, "url": url,
            "date_modified": _iso(modified), "wikidata_id": wikidata_id, "text_length": length,
            "languages": languages, "categories": categories, "history": history}


def _reverts(sha1s: list) -> int:
    """Versions identical to an earlier, non-adjacent version (and not to the one before)."""
    count, seen = 0, set()          # seen: sha1 of every version up to two back
    for i, s in enumerate(sha1s):
        if i >= 2:
            seen.add(sha1s[i - 2])
        if s and i >= 1 and s != sha1s[i - 1] and s in seen:
            count += 1
    return count


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, datetime) else str(v)
