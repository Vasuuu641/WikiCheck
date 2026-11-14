# Aikakone — Wikipedia, checked across languages and time

A browser extension for people who edit Wikipedia. On the article in the tab it shows:

- **Conflicts**: statements that contradict the same article in other language editions
- **Missing here**: important content the other editions have and this one lacks
- **Ask**: an answer with citations from Wikipedia passages, today or as Wikipedia stood on a past date
- **Drift**: how the article's position on its main claims changed over the years, down to the edit
  that changed it (popup, and a full page with the charts at `/drift`)

Built on MariaDB: native vector search finds the passages, and a system-versioned table holds every
version of the Finnish articles, so "as of" questions and drift are plain SQL.

Data: MariaDB's Wikipedia datasets (S = current text and vectors, M = every version of the Finnish
History-of-Finland articles). Wikipedia text is CC BY-SA 4.0: credit Wikipedia, share derived text alike.

## Layout

| Folder | What it is |
|---|---|
| `extension/` | Chrome extension (Manifest V3). Calls the server on `127.0.0.1:8020` |
| `api/` | The one server, FastAPI. `consistency.py` + `conflicts.py`: cross-language checks. `ask.py` + `search.py` + `rewrite.py` + `review.py` + `evidence.py`: Ask. `drift/`: opinion drift. `llm.py`: every model name and LLM call. `decisions.py`: Gemini Decisions API client |
| `db/` | Schema, applied by `make schema` (see `db/README.md`) |
| `eval/` | Measurements: retrieval quality (`run_eval.py`), conflict precision (`conflicts_eval.py`), results |
| `scripts/` | `smoke.py` (end-to-end test), `load_m.py` (dataset M), `ollama.sh`, `verda.py` (optional rented GPU) |
| `pacome/data/` | Git-ignored: dataset M, the Ollama binary and model |

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
make smoke        # in another terminal: the extension's and the drift page's endpoints, end to end
make check        # before a commit: compile and lint the Python, syntax-check the extension
```

Extension: Chrome, `chrome://extensions`, Developer mode, **Load unpacked**, choose `extension/`.
After a change, click its reload arrow. Try `https://sv.wikipedia.org/wiki/Regeringen_Aura_II`
(conflicts), `https://en.wikipedia.org/wiki/Hauho` (missing content) and
`https://fi.wikipedia.org/wiki/Talvisota` (drift).

## Settings

All in `.env` (template: `.env.example`, never commit `.env`). Model names have defaults in
`api/llm.py`: `CHECK_MODEL` for the cross-language checks (fast, runs on every page) and
`WRITE_MODEL` for drift. Ask uses the OpenAI-compatible `LLM_*` and `NEUTRALITY_*` endpoints.

## Working together

1. `main` must always pass `make check` and `make smoke`.
2. Small branches (`name/topic`), merged often.
3. Schema changes are new numbered files in `db/`; never edit an applied one.
4. No data and no secrets in git: data in `pacome/data/` (ignored), keys in `.env` (ignored).
5. Feature freeze: Sunday 2026-11-15, 07:30 Finnish time. Submit by 09:15.
