# Running the RAG demo

For someone who has just cloned the repo. About 15 minutes the first time, mostly downloads.

## What you need first

| Need | How to check | Install (Debian/Ubuntu) |
|---|---|---|
| Linux x86_64 | | macOS works too, but install Ollama from https://ollama.com/download first |
| Python 3.11+ | `python3 --version` | `sudo apt install python3 python3-venv` |
| MariaDB server 11.7+ | `mariadb --version` | `sudo apt install mariadb-server`. 11.7 is the first version with vector search; older distro packages need MariaDB's own repo |
| C connector headers | | `sudo apt install libmariadb-dev gcc`, so pip can build the `mariadb` package |
| git, curl, zstd | | `sudo apt install git curl zstd` |
| `sudo` rights | | used once, to create the `wikirag` database user |
| Disk | | about 4 GB (Ollama and the model 3.5 GB, data 0.5 GB) |
| Gemini API key | | free at https://aistudio.google.com/apikey. Without it search works, answers do not |

An NVIDIA GPU is optional: Ollama uses it if present, and embedding one question is fast on a CPU.

## Steps

```bash
git clone <this repo> && cd <repo>/pacome
cp .env.example .env            # then edit .env and set GOOGLE_API_KEY=...
scripts/setup.sh                # once
scripts/run.sh                  # every time, then open http://localhost:8000
```

`setup.sh` does five things and skips any that are already done, so it is safe to re-run:

1. creates `.venv/` and installs the Python packages
2. downloads Ollama into `data/tools/` (unless one is already installed) and pulls the `bge-m3` model
3. creates the MariaDB database and user `wikirag` / `wikirag` (asks for your sudo password)
4. clones MariaDB's starter repo into `reference/`
5. creates the tables and loads 29,460 articles with their ready-made vectors

`run.sh` starts Ollama if it is not running, then the web page. Pass `--port 8001` to change the port.

## Try it

- Web: open http://localhost:8000 and ask "Which peace treaty ended the Winter War?"
- Command line: `.venv/bin/python rag-demo/rag.py "Mikä oli Talvisota?" --lang fi`

You should see an answer citing `[1]`, `[2]`, then timings such as `embed 24 ms | MariaDB 6 ms | Gemini 2000 ms`.

## What runs where

| Part | Address | Started by |
|---|---|---|
| MariaDB | 127.0.0.1:3306 | the system service (`sudo systemctl start mariadb`) |
| Ollama | localhost:11434 | `scripts/run.sh` (log in `data/tools/ollama.log`) |
| Web page | localhost:8000 | `scripts/run.sh` |

## What is not in git

Everything big or private is git-ignored, and `setup.sh` re-creates it:

| Path | What | Size |
|---|---|---|
| `.env` | your API key | tiny |
| `.venv/` | Python packages | 100 MB |
| `data/` | Ollama binary and models, any downloaded datasets | 3.5 GB |
| `reference/` | clone of the starter repo and its data | 500 MB |
| `*.log` | logs | small |

Never commit `.env`. If a key leaks, revoke it at https://aistudio.google.com/apikey.

## Problems

| Symptom | Cause and fix |
|---|---|
| `pip` fails building `mariadb` | missing C connector: `sudo apt install libmariadb-dev gcc` |
| A package fails on Python 3.14 | use 3.12 or 3.13: `rm -rf .venv && python3.12 -m venv .venv`, then re-run `setup.sh` |
| `Can't connect to server on 127.0.0.1:3306` | MariaDB is stopped: `sudo systemctl start mariadb`. Different port: set `DB_PORT` in `.env` |
| Errors mentioning `VECTOR` or `mhnsw` | MariaDB older than 11.7 |
| Page shows `GOOGLE_API_KEY is not set` | add the key to `.env` and restart `run.sh` |
| Gemini returns 404 for the model | set `GEMINI_MODEL` in `.env` to a model your key can use |
| `Address already in use` | port 8000 is taken: `scripts/run.sh --port 8001` |
| Ollama will not start | read `data/tools/ollama.log`. If another Ollama already listens on 11434 it is reused, which is fine |

## Stopping

`Ctrl+C` stops the page. Ollama keeps running in the background; stop it with `pkill -x ollama`.
