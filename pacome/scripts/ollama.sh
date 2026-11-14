# Sourced by setup.sh and run.sh. Finds Ollama (project copy first, then system), starts it if
# needed, and keeps its models inside data/tools/models so nothing lands in your home directory.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/data/tools"
export OLLAMA_MODELS="$TOOLS/models"
# The local server the script starts and checks. The app reads OLLAMA_URL itself (.env), so it can point at a remote GPU.
LOCAL_OLLAMA_URL="http://localhost:11434"

find_ollama() {
  if [ -x "$TOOLS/ollama/bin/ollama" ]; then OLLAMA_BIN="$TOOLS/ollama/bin/ollama"
  elif command -v ollama >/dev/null; then OLLAMA_BIN="$(command -v ollama)"
  else return 1; fi
}

install_ollama() {   # Linux x86_64 only; elsewhere install Ollama from https://ollama.com/download
  command -v zstd >/dev/null || { echo "Missing: zstd (sudo apt install zstd)"; exit 1; }
  mkdir -p "$TOOLS/ollama"
  curl -fL https://ollama.com/download/ollama-linux-amd64.tar.zst | zstd -d | tar -x -C "$TOOLS/ollama"
  find_ollama
}

ollama_up() {
  curl -s -m2 "$LOCAL_OLLAMA_URL/api/tags" >/dev/null && return 0
  echo "Starting Ollama (log: data/tools/ollama.log)"
  mkdir -p "$TOOLS"
  nohup "$OLLAMA_BIN" serve > "$TOOLS/ollama.log" 2>&1 &
  for _ in $(seq 30); do curl -s -m2 "$LOCAL_OLLAMA_URL/api/tags" >/dev/null && return 0; sleep 1; done
  echo "Ollama did not start, see data/tools/ollama.log"; exit 1
}
