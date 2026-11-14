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


@app.post("/api/ask")
def ask(body: AskBody):
    return mock("ask") if MOCK else real_not_ready("ask")


@app.get("/api/article/{page_id}/changes")
def changes(page_id: int, from_: str | None = Query(None, alias="from"), to: str | None = None):
    return mock("changes") if MOCK else real_not_ready("changes")

@app.get("/api/search")
def api_search(q: str, limit: int = 10):
    from api.search import search
    hits, sql = search(q, limit)
    for h in hits:
        h["text"] = h["text"][:300]      # keep the response small
    return {"hits": hits, "sql": sql}

@app.post("/api/ask")
def ask(body: AskBody):
    if MOCK_ASK:
        return mock("ask")
    from api.ask import answer_with_citations
    return answer_with_citations(body.question)