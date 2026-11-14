#!/usr/bin/env bash
# One-time setup: Python venv, Ollama + bge-m3, MariaDB database and user, article data with vectors.
# Safe to re-run: every step is skipped when already done. Needs a MariaDB 11.7+ server running
# on 127.0.0.1:3306 (DB_PORT in .env to change) and, once, sudo to create the database user.
set -e
cd "$(dirname "$0")/.."
source scripts/ollama.sh
STARTER=reference/mariadb-vector-wikipedia-rag

need() { command -v "$1" >/dev/null || { echo "Missing: $1 ($2)"; exit 1; }; }
need python3 "Python 3.11+"; need git "git"; need curl "curl"; need mariadb "sudo apt install mariadb-server"

[ -f .env ] || cp .env.example .env
set -a; . ./.env; set +a
PORT="${DB_PORT:-3306}"

echo "1/5  Python venv"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r rag-demo/requirements.txt pyarrow duckdb || {
  echo "pip failed. The 'mariadb' package needs the C connector: sudo apt install libmariadb-dev gcc"; exit 1; }

echo "2/5  Ollama and the bge-m3 embedding model"
find_ollama || install_ollama
ollama_up
"$OLLAMA_BIN" list | grep -q '^bge-m3' || "$OLLAMA_BIN" pull bge-m3

echo "3/5  MariaDB database and user"
if ! mariadb -uwikirag -pwikirag -h127.0.0.1 -P"$PORT" wikirag -e 'select 1' >/dev/null 2>&1; then
  sudo mariadb -e "CREATE DATABASE IF NOT EXISTS wikirag;
    CREATE USER IF NOT EXISTS 'wikirag'@'localhost' IDENTIFIED BY 'wikirag';
    GRANT ALL ON wikirag.* TO 'wikirag'@'localhost'; FLUSH PRIVILEGES;"
fi

echo "4/5  Starter code (MariaDB's RAG starter, MIT licence)"
[ -d "$STARTER" ] || { mkdir -p reference
  git clone --depth 1 https://github.com/MariaDB/mariadb-vector-wikipedia-rag.git "$STARTER"; }
echo "DB_PORT=$PORT" > "$STARTER/.env"

echo "5/5  Tables, articles and vectors (downloads ~110 MB the first time)"
N=$(mariadb -uwikirag -pwikirag -h127.0.0.1 -P"$PORT" wikirag -N -e 'select count(*) from article' 2>/dev/null || echo 0)
if [ "${N:-0}" -gt 0 ]; then
  echo "     already loaded ($N articles)"
else
  (cd "$STARTER" && ../../.venv/bin/python create_tables.py && ../../.venv/bin/python load.py --vectors)
fi

echo; echo "Ready. Next: put GOOGLE_API_KEY in .env, then run  scripts/run.sh"
