#!/usr/bin/env bash
# Start the backend and the dashboard on macOS or Linux (the counterpart of run_app.ps1).
#
#   ./run_app.sh                          # rule-based decisions, no Ollama needed
#   ./run_app.sh --llm                    # decisions by the LLM (Ollama must be running)
#   ./run_app.sh --llm --model qwen3:4b   # pick the model
#   ./run_app.sh --date 2026-06-15        # a real day (ML forecasts + ENTSO-E prices)
#   ./run_app.sh --start 11:15            # pre-run the day up to 11:15
#
# Dashboard: http://localhost:5173 . Ctrl+C stops both.
# Uses .venv/bin/python if it exists, otherwise python3. Needs Node.js for the
# dashboard; without it the dashboard dev server runs in Docker instead.
set -euo pipefail

DATE=""
START="00:00"
BACKEND_PORT=8001
USE_LLM=false
MODEL=""

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)  DATE="$2"; shift 2 ;;
    --start) START="$2"; shift 2 ;;
    --port)  BACKEND_PORT="$2"; shift 2 ;;
    --llm)   USE_LLM=true; shift ;;
    --model) MODEL="$2"; USE_LLM=true; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)"
FRONTEND="$ROOT/frontend"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  echo "Python 3 not found. Install it, or create a venv: python3 -m venv .venv" >&2
  exit 1
fi

if ! "$PYTHON" -c "import fastapi, uvicorn, httpx" >/dev/null 2>&1; then
  echo "Python packages missing. Install them with: $PYTHON -m pip install -r requirements.txt" >&2
  exit 1
fi

BACKEND_ARGS=(-m backend --start "$START" --host 127.0.0.1 --port "$BACKEND_PORT")
if [[ "$USE_LLM" == true ]]; then
  [[ -n "$MODEL" ]] && BACKEND_ARGS+=(--model "$MODEL")
else
  BACKEND_ARGS+=(--no-llm)
fi
[[ -n "$DATE" ]] && BACKEND_ARGS+=(--date "$DATE")

PIDS=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  docker rm -f gitrekt-dashboard-dev >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "Starting the backend on http://127.0.0.1:$BACKEND_PORT ..."
(cd "$ROOT" && exec "$PYTHON" "${BACKEND_ARGS[@]}") &
PIDS+=($!)

echo "Dashboard: http://localhost:5173   (Ctrl+C stops everything)"
if command -v npm >/dev/null 2>&1; then
  if [[ ! -d "$FRONTEND/node_modules" ]]; then
    echo "Installing frontend dependencies..."
    (cd "$FRONTEND" && npm install)
  fi
  (cd "$FRONTEND" && API_URL="http://127.0.0.1:$BACKEND_PORT" exec npm run dev:api) &
  PIDS+=($!)
elif command -v docker >/dev/null 2>&1; then
  echo "Node.js not found: running the dashboard dev server in Docker."
  if ! docker info >/dev/null 2>&1; then
    echo "Docker is installed but not usable: start Docker, or on Linux add yourself to the" >&2
    echo "docker group (sudo usermod -aG docker \$USER, then log out and in)." >&2
    exit 1
  fi
  if [[ "$(uname -s)" == "Linux" ]]; then
    # On Linux the container shares the host's network: the backend listens on 127.0.0.1
    # only, which host.docker.internal (the docker bridge) cannot reach.
    docker run --rm --name gitrekt-dashboard-dev --network host \
      -v "$FRONTEND:/app" -v /app/node_modules -w /app \
      -e API_URL="http://127.0.0.1:$BACKEND_PORT" \
      node:20-alpine sh -c "npm ci --no-audit --no-fund && npm run dev:api -- --host 127.0.0.1 --port 5173" &
  else
    # Docker Desktop (macOS, Windows) forwards host.docker.internal to the host's localhost.
    docker run --rm --name gitrekt-dashboard-dev -p 5173:5173 \
      -v "$FRONTEND:/app" -v /app/node_modules -w /app \
      -e API_URL="http://host.docker.internal:$BACKEND_PORT" \
      --add-host host.docker.internal:host-gateway \
      node:20-alpine sh -c "npm ci --no-audit --no-fund && npm run dev:api -- --host 0.0.0.0" &
  fi
  PIDS+=($!)
else
  echo "Neither Node.js nor Docker found: install Node.js (https://nodejs.org) to run the dashboard." >&2
  exit 1
fi

wait -n "${PIDS[@]}" 2>/dev/null || wait
