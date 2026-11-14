# WikiCheck: one server (api/, FastAPI on :8020) behind the browser extension and the drift page.
#
#   make db-user     once: the database and its user (asks for your password: sudo)
#   make setup       everything else, safe to repeat: .env, Python venv, Ollama + bge-m3, tables,
#                    articles with vectors (~110 MB), edit history (dataset M, ~315 MB)
#   make run         start Ollama if needed, then the server on http://127.0.0.1:8020
#   make check       compile the Python, lint it, syntax-check the extension
#   make smoke       end-to-end test against the running server
#   make db-docker   a MariaDB in Docker, for machines without one (port 3307: set DB_PORT=3307 in .env)

PY = .venv/bin/python
PORT ?= 8020
ENV = set -a; [ -f .env ] && . ./.env; set +a
SQL = $(ENV); mariadb -u"$${DB_USER:-wikirag}" -p"$${DB_PASSWORD:-wikirag}" -h"$${DB_HOST:-127.0.0.1}" -P"$${DB_PORT:-3306}" "$${DB_NAME:-wikirag}"
OLLAMA = . scripts/ollama.sh
STARTER = pacome/reference/mariadb-vector-wikipedia-rag

setup: env venv ollama schema articles demo-data
	@echo; echo "Setup done. Start the server with: make run"

env:
	@[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "Created .env from .env.example: put the API keys in it"; }

db-user:
	sudo mariadb -e "CREATE DATABASE IF NOT EXISTS wikirag; \
	  CREATE USER IF NOT EXISTS 'wikirag'@'localhost' IDENTIFIED BY 'wikirag'; \
	  CREATE USER IF NOT EXISTS 'wikirag'@'127.0.0.1' IDENTIFIED BY 'wikirag'; \
	  GRANT ALL ON wikirag.* TO 'wikirag'@'localhost'; GRANT ALL ON wikirag.* TO 'wikirag'@'127.0.0.1'; FLUSH PRIVILEGES;"

venv:
	[ -d .venv ] || python3 -m venv .venv
	.venv/bin/pip install -q -r api/requirements.txt

ollama:
	@bash -c '$(OLLAMA); find_ollama || install_ollama; ollama_up; "$$OLLAMA_BIN" list | grep -q "^bge-m3" || "$$OLLAMA_BIN" pull bge-m3'

# Apply db/*.sql in order. Every file uses CREATE TABLE IF NOT EXISTS, so this is safe to repeat.
schema:
	@for f in $$(ls db/[0-9]*.sql | sort); do echo "applying $$f"; ($(SQL) < $$f) || exit 1; done

# Articles and their vectors (dataset S), loaded once with MariaDB's starter, which has its own venv.
articles:
	@[ -d $(STARTER) ] || git clone -q --depth 1 https://github.com/MariaDB/mariadb-vector-wikipedia-rag.git $(STARTER)
	@[ -d $(STARTER)/.venv ] || python3 -m venv $(STARTER)/.venv
	@$(STARTER)/.venv/bin/pip install -q -r $(STARTER)/requirements.txt
	@N=$$( ($(SQL) -N -e "SELECT COUNT(*) FROM article") 2>/dev/null || echo 0); \
	 if [ "$${N:-0}" -gt 0 ]; then echo "articles already loaded ($$N)"; \
	 else $(ENV); cd $(STARTER) && .venv/bin/python create_tables.py && .venv/bin/python load.py --vectors; fi

demo-data:
	$(PY) scripts/load_m.py
	$(PY) eval/load_history.py

run:
	@bash -c '$(OLLAMA); find_ollama && ollama_up || echo "Ollama not found: make ollama"'
	$(PY) -m uvicorn api.main:app --host 127.0.0.1 --port $(PORT)

check:
	$(PY) -m compileall -q api eval scripts
	$(PY) -m pyflakes api eval scripts
	@if command -v node >/dev/null; then for f in extension/*.js; do node --check $$f || exit 1; done; \
	 else echo "node not found: extension not syntax-checked"; fi

smoke:
	python3 scripts/smoke.py --base http://127.0.0.1:$(PORT)

db-docker:
	docker compose up -d --wait

.PHONY: setup env db-user venv ollama schema articles demo-data run check smoke db-docker
