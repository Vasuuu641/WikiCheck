# Aikakone — a time machine for Finnish Wikipedia

A map of the History of Finland on Wikipedia that you can travel through time and ask questions to,
with every answer linked to its source. Built on MariaDB (system-versioned tables + native vector search).

**One sentence for the jury:** Wikipedia only shows today's version; Aikakone shows what it said
then, what it says now, and where people argued — and MariaDB does both the remembering and the finding.

Data: MariaDB's Wikipedia datasets (S = current text + vectors, M = every version).
Wikipedia text is CC BY-SA 4.0: credit Wikipedia and share derived text under the same licence.

## Quick start (target: under 15 minutes)

```sh
cp .env.example .env
make up          # MariaDB in Docker on port 3307
make db          # create tables (db/*.sql)
make api-install # python deps
make api         # http://localhost:8000/docs  (serves MOCKS until you replace endpoints)
```

Frontend: see `web/README.md`. Data loading: see `db/README.md` and `pipeline/README.md`.

## Who owns what

| Area | Folder | Owner | One-line job |
|---|---|---|---|
| The memory | `db/`, `api/history.py` | Backend 1 | Store every past version; answer "what did it look like on date X" |
| The librarian | `api/search.py`, `api/ask.py`, `eval/` | _name_ | Find passages by meaning; write cited answers; measure quality |
| The map-maker | `pipeline/` | Backend 2 | Map coordinates, sampled revisions, then-vs-now summaries |
| The face | `web/` | Frontend | Map, time slider, ask box, SQL panel |

## Rules for working together

1. **The contract is `docs/API_CONTRACT.md`.** Change it only by telling the others. Mocks in `mocks/` follow it.
2. **Stay in your folder.** Need a change elsewhere? Ask the owner or open a small PR.
3. **Schema belongs to Backend 1.** Schema changes = a new numbered file in `db/`, never edit an applied one.
4. **Small branches, merge to `main` every couple of hours.** Name: `yourname/short-topic`.
5. **`main` must always start** with the quick start above. Test after you merge.
6. **No data and no secrets in git.** Datasets go in `data/` (ignored). Keys go in `.env` (ignored).
7. **Feature freeze: Sunday 07:30 Finnish time.** Submit by 09:15.

## The first hour (everyone)

1. Backend 1 + librarian: run `db/spike_vector_versioning.sql` — does a vector index work on a versioned table?
2. Frontend: build against `mocks/` using the API at `localhost:8000` (served by `api/main.py`).
3. Backend 2: clone MariaDB's dataset repo, pick 10-20 showcase articles, start the UMAP script.
