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

printf '==> Starting backend (Flask) on http://127.0.0.1:5000\n'
"$VENV_PYTHON" -m backend.app &
BACKEND_PID=$!

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

sleep 3
printf '==> Opening http://127.0.0.1:5173\n'
open_browser 'http://127.0.0.1:5173'

printf '\nBoth servers are running. Press Ctrl+C to stop.\n'
wait
