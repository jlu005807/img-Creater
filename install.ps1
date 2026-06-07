# Requires: PowerShell 5.1+, Python 3.10+, Node.js 18+
# One-click setup: creates the venv, installs backend + frontend deps.
# Usage:  .\install.ps1   (run from the project root)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Fail($msg)       { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

# --- Check prerequisites --------------------------------------------------
Write-Step 'Checking prerequisites'

$python = $null
foreach ($cmd in @('python', 'py')) {
  if (Get-Command $cmd -ErrorAction SilentlyContinue) { $python = $cmd; break }
}
if (-not $python) { Fail 'Python not found. Install Python 3.10+ and re-run.' }
$pyVersion = & $python --version 2>&1
Write-Ok "Python: $pyVersion"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Fail 'npm not found. Install Node.js 18+ (which bundles npm) and re-run.'
}
Write-Ok "Node: $(node --version)  npm: $(npm --version)"

# --- Backend: venv + deps -------------------------------------------------
Write-Step 'Setting up Python virtual environment (.venv)'
if (-not (Test-Path '.venv')) {
  & $python -m venv .venv
  if ($LASTEXITCODE -ne 0) { Fail 'Failed to create virtual environment.' }
  Write-Ok 'Created .venv'
} else {
  Write-Ok '.venv already exists'
}

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { Fail "venv python not found at $venvPython" }

Write-Step 'Installing backend dependencies'
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { Fail 'Backend dependency installation failed.' }
Write-Ok 'Backend dependencies installed'

# --- Frontend: deps -------------------------------------------------------
Write-Step 'Installing frontend dependencies (npm install)'
Push-Location frontend
try {
  npm install
  if ($LASTEXITCODE -ne 0) { Fail 'Frontend dependency installation failed.' }
} finally {
  Pop-Location
}
Write-Ok 'Frontend dependencies installed'

Write-Host "`nDone. Start everything with:  .\run.ps1" -ForegroundColor Green
