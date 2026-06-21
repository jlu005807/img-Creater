# Requires: PowerShell 5.1+, Python 3.10+, Node.js 18+
# One-click setup: creates the venv, installs backend + frontend deps.
# Usage:  .\install.ps1   (run from the project root)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Fail($msg)       { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Assert-VersionAtLeast($versionText, [int]$minMajor, [int]$minMinor, $name) {
  $match = [regex]::Match([string]$versionText, '(\d+)\.(\d+)(?:\.(\d+))?')
  if (-not $match.Success) { Fail "Could not parse $name version: $versionText" }
  $major = [int]$match.Groups[1].Value
  $minor = [int]$match.Groups[2].Value
  if ($major -lt $minMajor -or ($major -eq $minMajor -and $minor -lt $minMinor)) {
    Fail "$name $minMajor.$minMinor+ is required; found $versionText."
  }
}

# --- Check prerequisites --------------------------------------------------
Write-Step 'Checking prerequisites'

$python = $null
foreach ($cmd in @('python', 'py')) {
  if (Get-Command $cmd -ErrorAction SilentlyContinue) { $python = $cmd; break }
}
if (-not $python) { Fail 'Python not found. Install Python 3.10+ and re-run.' }
$pyVersionNumber = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
if ($LASTEXITCODE -ne 0) { Fail 'Unable to query Python version.' }
Assert-VersionAtLeast $pyVersionNumber 3 10 'Python'
Write-Ok "Python: $(& $python --version 2>&1)"

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  Fail 'Node.js not found. Install Node.js 18+ and re-run.'
}
$nodeVersionNumber = node -p "process.versions.node"
if ($LASTEXITCODE -ne 0) { Fail 'Unable to query Node.js version.' }
Assert-VersionAtLeast $nodeVersionNumber 18 0 'Node.js'

$npmCmd = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { 'npm.cmd' } elseif (Get-Command npm -ErrorAction SilentlyContinue) { 'npm' } else { $null }
if (-not $npmCmd) {
  Fail 'npm not found. Install Node.js 18+ (which bundles npm) and re-run.'
}
Write-Ok "Node: $(node --version)  npm: $(& $npmCmd --version)"

# --- Backend: venv + deps -------------------------------------------------
Write-Step 'Setting up Python virtual environment (.venv)'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
  if (Test-Path '.venv') {
    Write-Host "  .venv exists but the interpreter is missing — recreating" -ForegroundColor Yellow
    Remove-Item -Recurse -Force .venv
  }
  & $python -m venv .venv
  if ($LASTEXITCODE -ne 0) { Fail 'Failed to create virtual environment.' }
  Write-Ok 'Created .venv'
} else {
  Write-Ok '.venv already exists'
}
if (-not (Test-Path $venvPython)) { Fail "venv python not found at $venvPython" }

Write-Step 'Installing backend dependencies'
& $venvPython -m pip install --upgrade pip 2>$null
& $venvPython -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { Fail 'Backend dependency installation failed.' }
Write-Ok 'Backend dependencies installed'

# --- Frontend: deps -------------------------------------------------------
Write-Step 'Installing frontend dependencies (npm install)'
Push-Location frontend
try {
  & $npmCmd install
  if ($LASTEXITCODE -ne 0) { Fail 'Frontend dependency installation failed.' }
} finally {
  Pop-Location
}
Write-Ok 'Frontend dependencies installed'

# --- Seed local config from the template if missing -----------------------
$configPath = Join-Path $root 'backend\data\configs.json'
$examplePath = Join-Path $root 'backend\data\configs.example.json'
if (-not (Test-Path $configPath) -and (Test-Path $examplePath)) {
  Copy-Item $examplePath $configPath
  Write-Ok 'Created backend\data\configs.json from template'
}

New-Item -ItemType Directory -Force -Path (Join-Path $root 'history') | Out-Null
Write-Ok 'Ensured history directory exists'

Write-Host "`nDone." -ForegroundColor Green
Write-Host "Start everything with:  .\run.ps1  (it frees stale ports 5000/5173 first)" -ForegroundColor Green
Write-Host "Then open the gear icon (top-right) to add an API node." -ForegroundColor Green
