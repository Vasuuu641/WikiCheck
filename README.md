# WikiCheck — Wikipedia, checked across languages and time

**WikiCheck** is a Chrome extension and FastAPI service built for Wikipedia editors and curious readers. Open any Wikipedia article and WikiCheck immediately surfaces:

- **Conflicts**: factual contradictions between the current article and other language editions (e.g., differing dates, numbers, or claims), with direct highlights on the page.
- **Missing here**: important facts and sections present in other language editions that the current article lacks.
- **Find sources**: ask any question and get an answer grounded strictly in cited Wikipedia passages—either as Wikipedia stands today or as it stood on any past date—complete with a neutrality check on every source.
- **Version history**: trace how an article's position on key claims evolved over the years, down to the exact edit and diff (in the popup, and as an interactive timeline at `/drift`).

---

## The Challenge We Solved

Wikipedia is the world's largest collaborative knowledge base, yet its language editions often drift apart:

1. **Cross-language inconsistencies go unnoticed**: An article in English, Finnish, or Swedish can state conflicting figures or omit major context that another edition already has, because editors rarely read every language version side by side.
2. **Sourcing & neutrality take manual digging**: Checking what Wikipedia actually says across languages—and whether the wording is neutral or disputed—requires searching dozens of pages manually.
3. **Article history is opaque**: Standard revision logs show *what text changed*, not *when an article's stance on a claim shifted* (e.g., from disputed to stated as fact).

WikiCheck solves all three in one browser sidebar without leaving the article.

---

## How We Combined Both Tracks (RAG + SVT)

Instead of treating the **RAG** and **SVT** tracks separately, WikiCheck unifies them in one workflow:

- **RAG Track (MariaDB Native Vector Search)**: Article passages are embedded with `bge-m3` and queried via `VEC_DISTANCE_COSINE` + HNSW (`api/search.py`). **Find sources** (`api/ask.py`) retrieves the closest passages across English, Finnish, and Swedish, generates citation-backed answers with **Verda** (`kimi-k3-1-pro`), and runs **Gemini** (`gemini-3.8-flash`) to flag non-neutral wording or conflicting sources. **Conflicts** and **Missing here** (`api/consistency.py`) pair Wikidata links with vector similarity to catch contradictions and gaps across languages.
- **SVT Track (MariaDB System-Versioned Tables)**: Full revision histories of Finnish History-of-Finland articles live in `article_version` and are queried directly via MariaDB's native `FOR SYSTEM_TIME AS OF` SQL extension—no separate snapshot store needed. **Version history** (`/drift`) uses the **Gemini Decisions API** (`gemini-decision-1`) and binary-search bisection over SVT snapshots (`api/drift/stance.py`) to trace how an article's stance on key claims evolved over time, surface edit wars, and link directly to the exact Wikipedia diff.
- **RAG + SVT Together ("As Wikipedia stood on")**: In **Find sources**, picking a past date scopes RAG retrieval to SVT snapshots from that date—delivering cited answers as Wikipedia actually stood back then.

---

## Tech Partners & Architecture

| Partner / Tech | Where it's used | Why it makes WikiCheck effective |
|---|---|---|
| **MariaDB** (11.7+) | Native `VECTOR` search (`VEC_DISTANCE_COSINE` + HNSW) & system-versioned tables (`FOR SYSTEM_TIME AS OF`) | Powers semantic passage retrieval across languages using `bge-m3` embeddings, lets **Find sources** and **Version history** time-travel through historical article snapshots via MariaDB's native SQL extensions, and caches analyzed conflicts, gaps, and stance decisions. |
| **Google Gemini API** | `gemini-3.5-flash-lite`, `gemini-3.8-flash`, and the **Gemini Decisions API** (`gemini-decision-1`) | Runs fast cross-language conflict & gap detection (`CHECK_MODEL`), reviews cited passages for neutrality (`NEUTRALITY_*`), writes version-history summaries (`WRITE_MODEL`), and classifies historical claim stances via calibrated Decisions calls. |
| **Verda** | OpenAI-compatible inference (`kimi-k3-1-pro` via `LLM_*`, plus optional GPU setup in `scripts/verda.py`) | Synthesizes clear, citation-backed answers in **Find sources** strictly from the passages retrieved by MariaDB. |
| **Ollama (`bge-m3`)** | Local embedding service | Generates multilingual semantic vectors locally for articles and chunks without external embedding latency. |

Data: MariaDB's Wikipedia datasets (`S` = current text and vectors, `M` = full revision history of the Finnish History-of-Finland articles). Wikipedia text is CC BY-SA 4.0: credit Wikipedia, share derived text alike.

---

## What Makes It Scalable & Creative

- **Language-agnostic by design**: Articles are linked via Wikidata IDs and embedded with multilingual `bge-m3` vectors, so the same pipeline that compares English, Finnish, and Swedish extends naturally to any Wikipedia language edition.
- **Cache-first MariaDB storage**: Analyzed conflicts (`article_conflict`), missing content (`article_gap`), and revision stances (`stance_cache`) are stored in MariaDB. First-time analysis takes a few seconds; repeat visits return in milliseconds.
- **Edit-history bisection instead of brute force**: Rather than running an LLM on hundreds of revisions per article, **Version history** probes coarsely across time and binary-searches only the intervals where a stance changed—pinpointing the exact edit at minimal cost.
- **Right-sized model routing**: Fast, lightweight models handle per-page checks and stance classification, while larger reasoning/synthesis models run only when generating cited answers or summaries.

---

## Layout

| Folder | What it is |
|---|---|
| `extension/` | Chrome extension (Manifest V3). Calls the server on `127.0.0.1:8020` |
| `api/` | FastAPI server. `consistency.py` + `conflicts.py`: cross-language checks. `ask.py` + `search.py` + `rewrite.py` + `review.py` + `evidence.py`: Find sources. `drift/`: Version history. `llm.py`: model routing & LLM calls. `decisions.py`: Gemini Decisions API client |
| `db/` | Schema, applied by `make schema` (see `db/README.md`) |
| `eval/` | Retrieval quality (`run_eval.py`) and conflict precision (`conflicts_eval.py`) benchmarks |
| `scripts/` | `smoke.py` (end-to-end test), `load_m.py` (dataset M), `ollama.sh`, `verda.py` (optional rented GPU) |
| `pacome/data/` | Git-ignored: dataset M, the Ollama binary and model |

---

## First time

Needs Python 3.11+, a MariaDB 11.7+ server (or Docker: `make db-docker`, then `DB_PORT=3307`), and
a Gemini key. Linux x86_64 installs Ollama by itself; elsewhere install it from ollama.com.

```sh
make setup        # .venv, Ollama + bge-m3, tables; creates .env from .env.example
                  # fill GEMINI_API_KEY and LLM_* in .env
```

Load the articles and their vectors once, with MariaDB's starter (it brings its own Python packages):

```sh
git clone --depth 1 https://github.com/MariaDB/mariadb-vector-wikipedia-rag.git pacome/reference/mariadb-vector-wikipedia-rag
cd pacome/reference/mariadb-vector-wikipedia-rag
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "DB_PORT=3306" > .env && .venv/bin/python create_tables.py && .venv/bin/python load.py --vectors
cd -
make demo-data    # dataset M (~315 MB) and the edit history of the showcase articles
```

## Every day

```sh
make run          # Ollama if needed, then the server on http://127.0.0.1:8020 (/drift, /docs)
make smoke        # in another terminal: the extension's and the version history page's endpoints, end to end
make check        # before a commit: compile and lint the Python, syntax-check the extension
```

Extension: Chrome, `chrome://extensions`, Developer mode, **Load unpacked**, choose `extension/`.
After a change, click its reload arrow. Try `https://sv.wikipedia.org/wiki/Regeringen_Aura_II`
(conflicts), `https://en.wikipedia.org/wiki/Hauho` (missing content) and
`https://fi.wikipedia.org/wiki/Talvisota` (version history).

## Settings

All in `.env` (template: `.env.example`, never commit `.env`). Model names have defaults in
`api/llm.py`: `CHECK_MODEL` for the cross-language checks (fast, runs on every page) and
`WRITE_MODEL` for version history summaries. **Find sources** uses the OpenAI-compatible `LLM_*` and `NEUTRALITY_*` endpoints.

## Working together

1. `main` must always pass `make check` and `make smoke`.
2. Small branches (`name/topic`), merged often.
3. Schema changes are new numbered files in `db/`; never edit an applied one.
4. No data and no secrets in git: data in `pacome/data/` (ignored), keys in `.env` (ignored).
5. Feature freeze: Sunday 2026-11-15, 07:30 Finnish time. Submit by 09:15.
