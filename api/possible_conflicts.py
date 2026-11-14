"""GET /api/conflicts: possible factual conflicts between two articles, found offline by
eval/scan_conflicts.py (table possible_conflict, db/005_conflict.sql). Newest first."""
from .search import connect

MAX_LIMIT = 500

CONFLICTS_SQL = """
SELECT p.id, p.about, p.kind, p.status, p.distance, p.seed_category, p.model, p.created_at,
       p.page_id_a, p.chunk_no_a, p.phrase_a, aa.title, aa.url,
       p.page_id_b, p.chunk_no_b, p.phrase_b, ab.title, ab.url
FROM possible_conflict p
LEFT JOIN article aa ON aa.lang = 'fi' AND aa.page_id = p.page_id_a
LEFT JOIN article ab ON ab.lang = 'fi' AND ab.page_id = p.page_id_b
{where}
ORDER BY p.created_at DESC, p.id DESC
LIMIT %s"""


def _side(page_id, chunk_no, phrase, title, url) -> dict:
    return {"page_id": page_id, "chunk_no": chunk_no, "phrase": phrase, "title": title,
            "url": url or f"https://fi.wikipedia.org/?curid={page_id}",
            "edit_url": f"https://fi.wikipedia.org/w/index.php?curid={page_id}&action=edit"}


def list_conflicts(category: str | None = None, limit: int = 50) -> dict:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    where, params = ("WHERE p.seed_category = %s", [category]) if category else ("", [])
    with connect() as con, con.cursor() as cur:
        cur.execute(CONFLICTS_SQL.format(where=where), params + [limit])
        rows = cur.fetchall()
    items = [{"id": r[0], "about": r[1], "kind": r[2], "status": r[3], "distance": round(r[4], 4),
              "category": r[5], "model": r[6], "created_at": r[7].isoformat() if r[7] else None,
              "a": _side(*r[8:13]), "b": _side(*r[13:18])} for r in rows]
    return {"category": category, "count": len(items), "items": items}
