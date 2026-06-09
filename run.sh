#!/usr/bin/env bash
# Starts the Flask backend and the Vite dev server, then opens the browser.
# Usage:  ./run.sh   (run from the project root)
# Press Ctrl+C to stop both servers.
set -euo pipefail

cd "$(dirname "$0")"

VENV_PYTHON=".venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  printf '[ERROR] .venv not found. Run ./install.sh first.\n' >&2
  exit 1
fi
if [ ! -d frontend/node_modules ]; then
  printf '[ERROR] frontend/node_modules not found. Run ./install.sh first.\n' >&2
  exit 1
fi

BACKEND_PID=""
FRONTEND_PID=""
FRONTEND_PGID=""

stop_port_processes() {
  local port="$1"
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser "$port"/tcp 2>/dev/null || true)"
  fi
  [ -z "$pids" ] && return 0
  printf '  freeing port %s: stopping PID(s) %s\n' "$port" "$pids"
  kill $pids 2>/dev/null || true
  sleep 1
  kill -9 $pids 2>/dev/null || true
}

wait_http() {
  local url="$1"
  local expected="${2:-}"
  local max_wait="${3:-30}"
  local body=""
  for _ in $(seq 1 "$max_wait"); do
    body="$(curl -sf "$url" 2>/dev/null || true)"
    if [ -n "$body" ]; then
      if [ -z "$expected" ] || printf '%s' "$body" | grep -q "$expected"; then
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

# Kill the frontend's whole process group when possible: `npm run dev` forks
# node/vite as a grandchild, so signaling only the launcher leaves vite holding
# port 5173. setsid (Linux) puts it in its own group; otherwise fall back to PID.
cleanup() {
  [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null || true
  if [ -n "$FRONTEND_PGID" ]; then
    kill -- -"$FRONTEND_PGID" 2>/dev/null || true
  elif [ -n "$FRONTEND_PID" ]; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
  printf '\nStopped.\n'
}
trap cleanup EXIT INT TERM

open_browser() {
  local url="$1"
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "$url" >/dev/null 2>&1 || true
  fi
}

printf '==> Freeing old dev server ports (5000, 5173)\n'
stop_port_processes 5000
stop_port_processes 5173

printf '==> Starting backend (Flask) on http://127.0.0.1:5000\n'
# Run without the debug reloader for a clean one-shot launch.
FLASK_DEBUG=0 "$VENV_PYTHON" -m backend.app &
BACKEND_PID=$!

printf '==> Waiting for backend to be ready on http://127.0.0.1:5000\n'
if ! wait_http 'http://127.0.0.1:5000/api/health' 'img-Creater-backend' 30; then
  printf '[ERROR] Backend did not start with the current img-Creater code.\n' >&2
  exit 1
fi

printf '==> Starting frontend (Vite) on http://127.0.0.1:5173\n'
if command -v setsid >/dev/null 2>&1; then
  # Own session/group so cleanup can kill node/vite grandchildren too.
  setsid sh -c 'cd frontend && exec npm run dev' &
  FRONTEND_PID=$!
  FRONTEND_PGID=$FRONTEND_PID
else
  ( cd frontend && npm run dev ) &
  FRONTEND_PID=$!
fi

printf '==> Waiting for Vite to be ready on http://127.0.0.1:5173\n'
wait_http 'http://127.0.0.1:5173' '' 30 || printf '  Vite did not respond in 30s - opening browser anyway\n'
printf '==> Opening http://127.0.0.1:5173\n'
open_browser 'http://127.0.0.1:5173'

printf '\nBoth servers are running. Press Ctrl+C to stop.\n'
wait
