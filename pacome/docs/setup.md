# Setup

## This laptop, checked 2026-11-14

| Need | State |
|---|---|
| Python 3.11+ | 3.14.4 present |
| NVIDIA GPU | present (`nvidia-smi` works) |
| Disk | 401 GB free |
| RAM | 61 GB |
| Docker | missing |
| Ollama | missing |
| 7-Zip (`7zz` or `7z`) | missing. Only needed to load from raw Wikimedia dumps, not for dataset M's Parquet |
| `mariadb` client | missing. Optional, the starters connect from Python |

Python 3.14 is newer than what the starters were written against (3.11+). If the `mariadb`
connector or `pyarrow` fails to install, fall back to 3.12 or 3.13 in the venv.

The starters assume macOS (Homebrew, Apple GPU). On Linux, Ollama uses the NVIDIA GPU directly.

## What is needed for what

| | RAG starter | SVT starter | Datasets only |
|---|---|---|---|
| MariaDB | 11.7+ | 10.11+ | no |
| Docker | for MariaDB | for MariaDB | no |
| Ollama + bge-m3 | yes, to embed questions | no | no |
| 7-Zip | no | yes | only to rebuild M |
| Disk | about 3 GB | about 5 GB | S 156 MB, M 315 MB, L 495 MB |
| API keys | none | none | none |

## Reference clones

```bash
cd ~/Github/small-projects/Junction/mariadb-wikipedia
mkdir -p reference && cd reference
git clone --depth 1 https://github.com/MariaDB/mariadb-wikipedia-datasets.git
git clone --depth 1 https://github.com/MariaDB/mariadb-vector-wikipedia-rag.git      # 110 MB, ships S articles
git clone --depth 1 https://github.com/MariaDB/mariadb-system-versioning-wikipedia.git
```

`reference/` is git-ignored.

## MariaDB

Both starters run `mariadb:lts` through Docker Compose:

```bash
docker compose up -d --wait
```

| Starter | Database, user, password | Host port |
|---|---|---|
| RAG | `wikirag` | 3307 |
| SVT | `wikihistory` | 3308 |

To point a starter at another MariaDB, put `DB_PORT=...` in a `.env` file in its folder. The
scripts read `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`.

## RAG starter, first run

```bash
ollama pull bge-m3
cd reference/mariadb-vector-wikipedia-rag
./setup.sh                  # venv, MariaDB in Docker, articles, ready-made vectors
.venv/bin/python app.py     # http://localhost:8000
```

Or step by step:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # mariadb, requests, pyarrow, duckdb, flask
docker compose up -d --wait
python create_tables.py
python load.py                       # articles and categories, a few seconds
python load.py --vectors             # downloads 119 MB, about 3 min including index build
python search.py "Which peace treaty ended the Winter War?"
python evaluate.py --runs 5 --misses
```

Note: `flask` on port 8000 for both starters. Use `python app.py --port 8001` for the second.

## SVT starter, first run

```bash
cd reference/mariadb-system-versioning-wikipedia
./setup.sh                  # downloads about 3.4 GB of dumps, then loads for about 20 min
.venv/bin/python app.py
```

Or:

```bash
pip install -r requirements.txt      # mariadb, requests, flask
docker compose up -d --wait
python create_tables.py
python download.py                   # 12 dump files, 3.4 GB
python load.py                       # 50 articles, 31,848 versions
python asof.py Talvisota 2010-01-01 --sql
python -m unittest test_history
```

`setup.sh` mentions `brew install 7-zip`; on Linux the datasets repo says to install the `7zip`
package. The scripts call the binary `7zz` by name (hardcoded in `load.py` and `dataset_m.py`),
so check that the package provides it under that name.

## Datasets directly

Download the release files into `data/` (git-ignored) and verify with `SHA256SUMS`:

```bash
mkdir -p data && cd data
gh release download s-2026-10 -R MariaDB/mariadb-wikipedia-datasets -D s
gh release download m-2026-10 -R MariaDB/mariadb-wikipedia-datasets -D m
gh release download l-2026-10 -R MariaDB/mariadb-wikipedia-datasets -D l
(cd s && sha256sum -c SHA256SUMS)
```

Quick look without a database:

```bash
pip install duckdb
python -c "import duckdb; print(duckdb.sql(\"select page_id, title, count(*) n from 'm/versions-*.parquet' group by all order by n desc limit 10\"))"
```
