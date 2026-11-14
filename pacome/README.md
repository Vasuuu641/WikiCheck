# Wikipedia, History of Finland: a MariaDB Vector site

Junction 2026, MariaDB x Wikipedia challenge. A small web site over the Finnish, Swedish and English
Wikipedia articles on the history of Finland, with MariaDB doing the work: **vector search** (find text
by meaning, in any of the three languages) and **system-versioned tables** (every past version of an
article, queryable by date).

The site has two areas:

| Area | Page | What it does |
|---|---|---|
| **Readers** | Ask Wikipedia `/ask` | Ask in English, Finnish or Swedish; get a short answer citing the articles it comes from |
| | Opinion drift `/drift` | How the Finnish articles' position on the same claims changed over the years, down to the exact edit |
| | Language mirror `/mirror` | Which topics two languages tell most differently; read both side by side |
| | Closest neighbours `/neighbors` | Walk from an article to the ones closest in meaning, in any language |
| | The numbers `/stats` | What is in the database |
| **Editors** | Cross-language gaps `/gaps` | Sections another language has that the Finnish article lacks (no AI involved) |
| | Reference borrower `/refs` | For an unsourced Finnish paragraph, the same fact elsewhere with its reference |
| | Link suggester `/links` | Articles close in meaning but not linked, and where the link fits |
| | Consistency checker `/consistency` | Two articles giving different values for the same fact, side by side |

Every page has a **"? What is this page?"** button next to its title that explains how to use it, how it
works and its limits, and every page shows the exact SQL MariaDB ran.

## How it works

```
                    question or article
                            |
              bge-m3 via Ollama (local) -> a vector: 1,024 numbers that capture meaning
                            |
   MariaDB 11.7+  ----------+-------------------------------------------------------------
     article, article_vector        one vector per article          Ask, Mirror, Neighbours, Links
     chunk, chunk_vector            one vector per section          Gaps, Consistency
     para, para_vector, para_ref    one per Finnish paragraph       References, Links
     article_version (system-versioned): every past version of the Finnish articles      Drift
                            |
              Google AI (only where text has to be written or judged):
                Gemini writes answers, claims and comparison notes;
                yes/no checks ("same fact?", "incompatible values?") score candidates
                            |
                     Flask web site (rag-demo/app.py)
```

The vector search always sits in a subquery so MariaDB uses its vector index (about 1 ms instead of 50 ms
for a flat scan). Texts are never translated: bge-m3 puts all three languages in the same space.

## Run it

You need Linux or macOS, Python 3.11+, MariaDB server 11.7+, `libmariadb-dev gcc git curl zstd`,
about 4 GB of disk, and a free Google AI key from https://aistudio.google.com/apikey.
Exact install commands and fixes for common errors: [docs/running.md](docs/running.md).

```bash
git clone -b pacome git@github.com:Vasuuu641/rewrite.git && cd rewrite/pacome   # branch pacome until merged
cp .env.example .env        # then put your key after GOOGLE_API_KEY=
scripts/setup.sh            # once, ~15 min: Python packages, Ollama + bge-m3, database, 29,460 articles
scripts/run.sh              # every time; open http://localhost:8000  (--port 8001 to change)
```

That is enough for Home, Ask, Neighbours and Numbers, and for Opinion drift's **replay the demo**.
The other pages need extra tables, built once. `chunks.py` and `paras.py` embed text, so Ollama must be running
(start `scripts/run.sh` first and use a second terminal). Each build is resumable: stop it and run it again later.

| For | Run once | What it adds |
|---|---|---|
| Language mirror | `.venv/bin/python rag-demo/mirror.py build` | map positions of every article (`mirror_point`) |
| Gaps, Consistency | `.venv/bin/python rag-demo/chunks.py build` | one vector per section (`chunk`, `chunk_vector`) |
| Opinion drift (live), References, Links | `.venv/bin/python scripts/load_m.py` | downloads dataset M (every version of the Finnish articles) into `data/m/` |
| References, Links | `.venv/bin/python rag-demo/paras.py build --root "Suomen sodat"` | Finnish paragraphs, their references and links |

`chunks.py status` and `paras.py status` show how far a build got.

### Settings (`.env`)

| Name | Needed for | Default |
|---|---|---|
| `GOOGLE_API_KEY` | answers, notes, yes/no checks. Without it search still works | none |
| `GEMINI_MODEL` | which Gemini model writes | `gemini-3.8-flash` |
| `DB_HOST` `DB_PORT` `DB_USER` `DB_PASSWORD` `DB_NAME` | MariaDB | `127.0.0.1`, `3306`, `wikirag` ×3 |
| `OLLAMA_URL` | where bge-m3 runs | `http://localhost:11434` |
| `VERDA_CLIENT_ID` `VERDA_CLIENT_SECRET` | only `scripts/verda.py` (optional rented GPU for faster builds, billed per hour) | none |

## Keep your keys safe

- Keys live **only** in `.env`, which is git-ignored (so are `.env.*`, `*.pem`, `*.key`). Nothing in the
  code contains a key; it is read from `.env` at start-up.
- Before pushing, `git status` should never list `.env`. To double-check what a commit contains:
  `git diff --cached --name-only`.
- If a key ever reaches GitHub, deleting the file is not enough (it stays in history): revoke the key at
  https://aistudio.google.com/apikey and make a new one.
- Every AI call costs quota. Answers are cached where possible (yes/no checks in `pair_check`, stance scores
  in MariaDB), and Opinion drift's **replay the demo** costs nothing.

## Layout

```
pacome/
  README.md          this file
  .env.example       template for .env (copy it, never commit .env)
  rag-demo/          the web site
    app.py             starts it; Ask page
    guide.py           menu, Readers/Editors areas, the "What is this page?" text for every page
    hub.py             home, /readers, /editors, /stats
    drift.py stance.py history.py decisions.py   Opinion drift
    mirror.py neighbors.py                       Language mirror, Closest neighbours
    gaps.py refs.py links.py consistency.py tb.py   the editor tools
    rag.py chunks.py paras.py                    search, embeddings, the section and paragraph tables
  scripts/           setup.sh (once), run.sh (every time), load_m.py, verda.py, evaluation scripts
  docs/              background: the brief, datasets, MariaDB Vector, versioned tables, test results
  data/  reference/  .venv/   made by setup.sh, git-ignored
```

## More docs

| File | What it holds |
|---|---|
| [docs/running.md](docs/running.md) | Step-by-step setup on a fresh machine, and fixes for common errors |
| [rag-demo/README.md](rag-demo/README.md) | The Ask page: design choices and known limits |
| [docs/tier-b-checks.md](docs/tier-b-checks.md) | How the editor tools' thresholds were chosen, and how often they are right |
| [docs/challenge.md](docs/challenge.md) | The challenge brief |
| [docs/datasets.md](docs/datasets.md) | The three datasets (S, M, L) |
| [docs/rag-track.md](docs/rag-track.md) | MariaDB Vector: schema, queries, index tuning |
| [docs/svt-track.md](docs/svt-track.md) | System-versioned tables: loading real timestamps, time-travel queries |
| [docs/combining.md](docs/combining.md) | Joining the two |

Sources: [mariadb-wikipedia-datasets](https://github.com/MariaDB/mariadb-wikipedia-datasets),
[mariadb-vector-wikipedia-rag](https://github.com/MariaDB/mariadb-vector-wikipedia-rag),
[mariadb-system-versioning-wikipedia](https://github.com/MariaDB/mariadb-system-versioning-wikipedia).

## Licence

Wikipedia text, and anything derived from it such as vectors, is CC BY-SA 4.0: credit Wikipedia and share
derivatives under the same licence. MariaDB's starter code is MIT.
