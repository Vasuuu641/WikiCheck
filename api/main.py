"""Aikakone: the one server behind the browser extension and the drift page.

  GET  /drift, /api/drift/...                       opinion drift (api/drift/routes.py)
  GET  /api/consistency, /api/consistency/search   conflicts and missing content across language editions
  POST /api/ask                                     cited answer from MariaDB passages, today or as of a date
  GET  /api/search                                  nearest articles to a query (debugging)
  POST /api/consistency/analyze, /invalidate        recompute or forget one topic (maintenance)
"""
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

load_dotenv()

from .drift.routes import router as drift_router  # noqa: E402  (reads .env at import)

app = FastAPI(title="Aikakone")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(drift_router)


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/drift")


@app.get("/api/health")
def health():
    return {"ok": True}


class AskBody(BaseModel):
    question: str
    as_of: str | None = None
    mode: str = "reader"   # "reader" | "editor" (see api/ask.py answer)
    lang: str | None = None  # "en" | "fi" | "sv": write the answer and review reasons in it


@app.post("/api/ask")
def ask(body: AskBody, debug: bool = False):
    """Answer with citations; each citation has `neutrality`; in mode "editor" also `edit_url` per
    citation and `conflicts`. With ?debug=1 the response also has a `process` field (what each step
    found and how long it took)."""
    from . import ask as ask_mod

    trace: dict = {}
    try:
        result = ask_mod.answer(body.question, body.as_of, trace=trace if debug else None,
                                mode=body.mode, lang=body.lang)
    except NotImplementedError as e:
        return JSONResponse({"error": str(e)}, status_code=501)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # DB, Ollama or LLM unreachable
        return JSONResponse({"error": f"ask failed: {type(e).__name__}: {e}"}, status_code=502)
    return {**result, "process": trace} if debug else result


@app.get("/api/search")
def api_search(q: str, limit: int = 10, lang: str | None = None):
    """Nearest articles to a query, with the SQL; `lang` = fi | sv | en searches one language."""
    from .search import search
    if lang not in (None, "fi", "sv", "en"):
        return JSONResponse({"error": "lang must be fi, sv or en"}, status_code=400)
    hits, sql = search(q, limit, with_text=True, lang=lang)
    for h in hits:
        h["text"] = h["text"][:300]      # keep the response small
    return {"hits": hits, "sql": sql}


# --- Cross-language consistency for the browser extension (api/consistency.py)

@app.get("/api/consistency")
def consistency(
    lang: str,
    title: str | None = None,
    page_id: int | None = None,
    compute: bool = True,
    refresh: bool = False,
):
    from api.consistency import get_consistency
    return get_consistency(lang=lang, title=title, page_id=page_id,
                           compute_if_missing=compute, refresh=refresh)


class AnalyzeBody(BaseModel):
    wikidata_id: str
    force: bool = False


@app.get("/api/consistency/search")
def consistency_search(q: str, limit: int = 8):
    from api.consistency import search_consistency
    return search_consistency(q=q, limit=limit)


@app.post("/api/consistency/analyze")
def consistency_analyze(body: AnalyzeBody):
    from api.consistency import analyze_entity
    return analyze_entity(body.wikidata_id, force=body.force)


class InvalidateBody(BaseModel):
    wikidata_id: str | None = None
    lang: str | None = None
    title: str | None = None


@app.post("/api/consistency/invalidate")
def consistency_invalidate(body: InvalidateBody):
    from api.consistency import invalidate_entity
    return {"invalidated": invalidate_entity(body.wikidata_id, body.lang, body.title)}
