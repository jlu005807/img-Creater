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
VENV_PYTHON=".venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  if [ -d .venv ]; then
    printf '  .venv exists but the interpreter is missing — recreating\n'
    rm -rf .venv
  fi
  "$PYTHON" -m venv .venv
  ok 'Created .venv'
else
  ok '.venv already exists'
fi
[ -x "$VENV_PYTHON" ] || fail "venv python not found at $VENV_PYTHON"

step 'Installing backend dependencies'
"$VENV_PYTHON" -m pip install --upgrade pip || true  # non-critical
"$VENV_PYTHON" -m pip install -r backend/requirements.txt
ok 'Backend dependencies installed'

step 'Installing frontend dependencies (npm install)'
( cd frontend && npm install )
ok 'Frontend dependencies installed'

# Seed local config from the template if missing.
if [ ! -f backend/data/configs.json ] && [ -f backend/data/configs.example.json ]; then
  cp backend/data/configs.example.json backend/data/configs.json
  ok 'Created backend/data/configs.json from template'
fi

mkdir -p history
ok 'Ensured history directory exists'

printf '\nDone.\n'
printf 'Start everything with:  ./run.sh  (it frees stale ports 5000/5173 first)\n'
printf 'Then open the gear icon (top-right) to add an API node.\n'
