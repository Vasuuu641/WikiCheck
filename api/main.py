"""Aikakone API. Serves mocks/ while MOCK=1 so the frontend can start immediately.

Owners replace one endpoint at a time with real queries (set MOCK=0 and fill the real branch):
  /api/map, /api/timeline, /api/hot  -> api/history.py (Backend 1)
  /api/ask                           -> api/search.py + api/ask.py (librarian)
  /api/article/{id}/changes          -> pipeline output in change_summary (Backend 2)
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()
MOCK = os.getenv("MOCK", "1") == "1"
MOCKS = Path(__file__).resolve().parent.parent / "mocks"

app = FastAPI(title="Aikakone")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def mock(name: str):
    return json.loads((MOCKS / f"{name}.json").read_text())


def real_not_ready(what: str):
    raise HTTPException(501, f"{what}: real implementation not written yet (set MOCK=1)")


@app.get("/", include_in_schema=False)
def ask_test_page():
    """Simple test page for /api/ask (the real frontend lives in web/)."""
    return FileResponse(Path(__file__).resolve().parent / "static" / "ask.html")


@app.get("/api/health")
def health():
    return {"ok": True, "mock": MOCK}


@app.get("/api/map")
def get_map():
    return mock("map") if MOCK else real_not_ready("map")


@app.get("/api/timeline")
def timeline(year: int):
    return {**mock("timeline"), "year": year} if MOCK else real_not_ready("timeline")


@app.get("/api/hot")
def hot(year: int, limit: int = 10):
    return {**mock("hot"), "year": year} if MOCK else real_not_ready("hot")


class AskBody(BaseModel):
    question: str
    as_of: str | None = None
    mode: str = "reader"   # "reader" | "editor" (see api/ask.py answer)
    lang: str | None = None  # "en" | "fi" | "sv": write the answer and review reasons in it


@app.post("/api/ask")
def ask(body: AskBody, debug: bool = False):
    """Contract shape, plus additions: each citation has `neutrality`; in mode "editor" also
    `edit_url` per citation and `conflicts`. With ?debug=1 the response also has a `process`
    field (what each step found and how long it took)."""
    if MOCK:
        return mock("ask")
    from . import ask as ask_mod  # imported lazily so MOCK=1 needs no DB, Ollama or LLM settings

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


class CheckBody(BaseModel):
    text: str
    lang: str | None = None  # "en" | "fi" | "sv": write the review's reasons and problems in it


@app.post("/api/check")
def check(body: CheckBody, debug: bool = False):
    """For editors: neutrality review of a draft sentence, plus the Wikipedia passages that
    support or contradict it. Not in docs/API_CONTRACT.md yet."""
    if MOCK:
        return JSONResponse({"error": "/api/check has no mock; set MOCK=0"}, status_code=501)
    from .check import check as do_check

    trace: dict = {}
    try:
        result = do_check(body.text, trace=trace if debug else None, lang=body.lang)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"check failed: {type(e).__name__}: {e}"}, status_code=502)
    return {**result, "process": trace} if debug else result


class TranslateBody(BaseModel):
    text: str
    target: str = "en"


@app.post("/api/translate")
def translate(body: TranslateBody):
    """Helper for the test page: translate a source passage. Not part of docs/API_CONTRACT.md."""
    from .translate import translate as do_translate

    try:
        return {"translation": do_translate(body.text, body.target), "target": body.target}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"translate failed: {type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/article/{page_id}/info")
def article_info(page_id: int, lang: str = "fi"):
    """For the page's info box: one article's metadata, languages, categories and history.
    Not in docs/API_CONTRACT.md yet."""
    if MOCK:
        return JSONResponse({"error": "/api/article/{id}/info has no mock; set MOCK=0"}, status_code=501)
    from .article_info import info

    try:
        result = info(page_id, lang)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"info failed: {type(e).__name__}: {e}"}, status_code=502)
    if result is None:
        return JSONResponse({"error": f"no {lang} article with page_id {page_id}"}, status_code=404)
    return result


class TraceBody(BaseModel):
    page_id: int
    phrase: str


@app.post("/api/trace")
def trace(body: TraceBody):
    """For editors: when a phrase first appeared in an article, the edit that introduced the
    claim, and the runs of versions that contain it, from the stored history (api/trace.py).
    {available: false} when the article has no history. Not in docs/API_CONTRACT.md yet."""
    if MOCK:
        return JSONResponse({"error": "/api/trace has no mock; set MOCK=0"}, status_code=501)
    from .trace import trace as do_trace

    try:
        return do_trace(body.page_id, body.phrase)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"trace failed: {type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/trace/available")
def trace_available(page_ids: str = ""):
    """Which of the given (Finnish) page_ids have stored history: {"page_ids": [...]}."""
    if MOCK:
        return {"page_ids": []}
    from .trace import available

    try:
        ids = [int(x) for x in page_ids.split(",") if x.strip()]
    except ValueError:
        return JSONResponse({"error": "page_ids must be comma-separated integers"}, status_code=400)
    try:
        return {"page_ids": available(ids)}
    except Exception as e:
        return JSONResponse({"error": f"trace failed: {type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/conflicts")
def conflicts(category: str | None = None, limit: int = 50):
    """For editors: possible factual conflicts between two articles, found offline by
    eval/scan_conflicts.py. Newest first; `category` = the category the scan was run on.
    Not in docs/API_CONTRACT.md yet."""
    if MOCK:
        return JSONResponse({"error": "/api/conflicts has no mock; set MOCK=0"}, status_code=501)
    from .possible_conflicts import list_conflicts

    try:
        return list_conflicts(category, limit)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"conflicts failed: {type(e).__name__}: {e}"}, status_code=502)


@app.get("/api/article/{page_id}/changes")
def changes(page_id: int, from_: str | None = Query(None, alias="from"), to: str | None = None):
    return mock("changes") if MOCK else real_not_ready("changes")


@app.get("/api/search")
def api_search(q: str, limit: int = 10):
    """Nearest articles to a query, with the SQL. Not in docs/API_CONTRACT.md yet."""
    from .search import search
    hits, sql = search(q, limit, with_text=True)
    for h in hits:
        h["text"] = h["text"][:300]      # keep the response small
    return {"hits": hits, "sql": sql}


# --- Cross-language consistency for the browser extension (api/consistency.py). Always real: no mock.

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
