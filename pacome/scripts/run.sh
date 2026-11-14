#!/usr/bin/env bash
# Start everything the demo needs and serve it on http://localhost:8000  (scripts/run.sh --port 8001)
set -e
cd "$(dirname "$0")/.."
source scripts/ollama.sh
[ -x .venv/bin/python ] || { echo "Run scripts/setup.sh first"; exit 1; }
find_ollama || { echo "Ollama not found. Run scripts/setup.sh first"; exit 1; }
systemctl is-active --quiet mariadb 2>/dev/null || echo "Warning: the mariadb service is not active (sudo systemctl start mariadb)"
grep -q '^GOOGLE_API_KEY=.' .env 2>/dev/null || echo "Warning: GOOGLE_API_KEY missing in .env, answers will fail (retrieval still works)"
ollama_up
exec .venv/bin/python rag-demo/app.py "$@"
