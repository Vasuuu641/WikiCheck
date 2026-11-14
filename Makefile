# Aikakone: one server (api/, FastAPI on :8020) behind the browser extension and the drift page.
#
#   make setup       first time: Python venv, Ollama + bge-m3, database tables
#   make demo-data   dataset M (Finnish edit history, ~315 MB) and the showcase histories
#   make run         start Ollama if needed, then the server on http://127.0.0.1:8020
#   make check       compile the Python, lint it, syntax-check the extension
#   make smoke       end-to-end test against the running server
#   make db-docker   a MariaDB in Docker, for machines without one (port 3307: set DB_PORT=3307 in .env)

PY = .venv/bin/python
PORT ?= 8020
ENV = set -a; [ -f .env ] && . ./.env; set +a
SQL = $(ENV); mariadb -u"$${DB_USER:-wikirag}" -p"$${DB_PASSWORD:-wikirag}" -h"$${DB_HOST:-127.0.0.1}" -P"$${DB_PORT:-3306}" "$${DB_NAME:-wikirag}"
OLLAMA = . scripts/ollama.sh

setup: venv ollama schema
	@[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "Created .env: fill GEMINI_API_KEY and LLM_*"; }
	@echo "Next: load the articles once (README, 'First time'), then make demo-data and make run"

venv:
	[ -d .venv ] || python3 -m venv .venv
	.venv/bin/pip install -q -r api/requirements.txt

ollama:
	@bash -c '$(OLLAMA); find_ollama || install_ollama; ollama_up; "$$OLLAMA_BIN" list | grep -q "^bge-m3" || "$$OLLAMA_BIN" pull bge-m3'

# Apply db/*.sql in order. Every file uses CREATE TABLE IF NOT EXISTS, so this is safe to repeat.
schema:
	@for f in $$(ls db/[0-9]*.sql | sort); do echo "applying $$f"; ($(SQL) < $$f) || exit 1; done

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

.PHONY: setup venv ollama schema demo-data run check smoke db-docker
