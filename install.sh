#!/usr/bin/env bash
# One-click setup for Linux/macOS: creates the venv, installs backend + frontend deps.
# Usage:  ./install.sh   (run from the project root)
# Requires: Python 3.10+, Node.js 18+
set -euo pipefail

cd "$(dirname "$0")"

step() { printf '\n==> %s\n' "$1"; }
ok()   { printf '[OK] %s\n' "$1"; }
fail() { printf '[ERROR] %s\n' "$1" >&2; exit 1; }

step 'Checking prerequisites'
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then PYTHON="$cmd"; break; fi
done
[ -n "$PYTHON" ] || fail 'Python not found. Install Python 3.10+ and re-run.'
ok "Python: $($PYTHON --version 2>&1)"

command -v npm >/dev/null 2>&1 || fail 'npm not found. Install Node.js 18+ and re-run.'
ok "Node: $(node --version)  npm: $(npm --version)"

step 'Setting up Python virtual environment (.venv)'
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
  ok 'Created .venv'
else
  ok '.venv already exists'
fi

VENV_PYTHON=".venv/bin/python"
[ -x "$VENV_PYTHON" ] || fail "venv python not found at $VENV_PYTHON"

step 'Installing backend dependencies'
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r backend/requirements.txt
ok 'Backend dependencies installed'

step 'Installing frontend dependencies (npm install)'
( cd frontend && npm install )
ok 'Frontend dependencies installed'

printf '\nDone. Start everything with:  ./run.sh\n'
