# Aikakone — a time machine for Finnish Wikipedia

A map of the History of Finland on Wikipedia that you can travel through time and ask questions to,
with every answer linked to its source. Built on MariaDB (system-versioned tables + native vector search).

**One sentence for the jury:** Wikipedia only shows today's version; Aikakone shows what it said
then, what it says now, and where people argued — and MariaDB does both the remembering and the finding.

Data: MariaDB's Wikipedia datasets (S = current text + vectors, M = every version).
Wikipedia text is CC BY-SA 4.0: credit Wikipedia and share derived text under the same licence.

## What is in here

The product is a **browser extension** for people who edit Wikipedia. On any article it shows
cross-language conflicts and missing content, answers questions with citations (also "as of" a past
date), and shows how the article's position on its main claims drifted, edit by edit.

| Part | Folder | Runs on | What it does |
|---|---|---|---|
| Extension | `extension/` | Chrome (load unpacked) | Check, Ask and Drift for the article in the tab |
| API | `api/` | FastAPI, `127.0.0.1:8020` | `/api/consistency*` (conflicts, gaps), `/api/ask`, `/api/check`, `/api/trace`, `/api/search` |
| Site | `pacome/` | Flask, `127.0.0.1:8001` | Opinion drift and the full-page tools (mirror, gaps, references, links); the extension's "full view" |
| Schema | `db/` | MariaDB 11.7+ | see `db/README.md` |
| Measurements | `eval/` | | retrieval quality, conflict precision, loaders |

One MariaDB database (`wikirag`) serves both servers. One RAG: `api/search.py` + `api/ask.py`
(paragraph passages, query rewriting, rank fusion, `as_of`). One history table: `article_version`.

## Quick start

1. Database, data, Ollama and the site: follow [pacome/README.md](pacome/README.md)
   (`pacome/scripts/setup.sh`, then `pacome/scripts/run.sh --port 8001`).
2. API:
   ```sh
   cp .env.example .env     # DB_PORT (3306 for a system MariaDB, 3307 for `make up`), keys, MOCK=0
   make api-install
   mariadb -uwikirag -pwikirag -h127.0.0.1 -P3306 wikirag < db/004_schema_consistency.sql   # also 005, 006
   make api                 # http://127.0.0.1:8020/docs
   ```
3. Extension: Chrome, `chrome://extensions`, Developer mode, **Load unpacked**, choose `extension/`.
   Open `https://sv.wikipedia.org/wiki/Regeringen_Aura_II` or `https://fi.wikipedia.org/wiki/Talvisota`
   and click the Aikakone icon.

## Rules for working together

1. **The contract is `docs/API_CONTRACT.md`.** Change it only by telling the others. Mocks in `mocks/` follow it.
2. **Stay in your folder.** Need a change elsewhere? Ask the owner or open a small PR.
3. **Schema belongs to Backend 1.** Schema changes = a new numbered file in `db/`, never edit an applied one.
4. **Small branches, merge to `main` every couple of hours.** Name: `yourname/short-topic`.
5. **`main` must always start** with the quick start above. Test after you merge.
6. **No data and no secrets in git.** Datasets go in `data/` (ignored). Keys go in `.env` (ignored).
7. **Feature freeze: Sunday 07:30 Finnish time.** Submit by 09:15.
