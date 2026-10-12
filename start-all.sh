#!/usr/bin/env bash
# Starts everything for the demo in the background: Phoenix, the detector, the
# agent worker and the UI. Logs and pids go to .run/. Stop with ./stop-all.sh.
# Needs the cluster up and a filled .env (./create-env.sh).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p .run
PY=".venv/bin"

start() {  # start <name> <command...>
  local name="$1"; shift
  if [[ -f ".run/$name.pid" ]] && kill -0 "$(cat ".run/$name.pid")" 2>/dev/null; then
    echo "$name: already running"; return
  fi
  nohup "$@" > ".run/$name.log" 2>&1 &
  echo $! > ".run/$name.pid"
  echo "$name: started (log: .run/$name.log)"
}

[[ -f .env ]] || { echo "No .env yet. Run ./create-env.sh first." >&2; exit 1; }
[[ -d ui/web/dist ]] || echo "Note: ui/web/dist is missing; run 'cd ui/web && npm install && npm run build' for the UI."

[[ -x "$PY/phoenix" ]] && start phoenix "$PY/phoenix" serve || echo "phoenix: not installed (uv sync --extra tracing); tracing skipped"
start detector "$PY/python" -u agent/detector.py
start worker   "$PY/python" -u -m agent.worker
start ui       "$PY/uvicorn" ui.app:app --port 8000

echo
echo "UI:      http://localhost:8000"
echo "Phoenix: http://localhost:6006"
