# Requires: PowerShell 5.1+, completed install (.\install.ps1)
# Starts the Flask backend and the Vite dev server, then opens the browser.
# Usage:  .\run.ps1
# Press Ctrl+C in this window to stop both servers.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
  Write-Host "[ERROR] .venv not found. Run .\install.ps1 first." -ForegroundColor Red
  exit 1
}
if (-not (Test-Path (Join-Path $root 'frontend\node_modules'))) {
  Write-Host "[ERROR] frontend\node_modules not found. Run .\install.ps1 first." -ForegroundColor Red
  exit 1
}

$backend = $null
$frontend = $null

# Stop child processes when this script exits (Ctrl+C or normal end).
$cleanup = {
  foreach ($p in @($script:backend, $script:frontend)) {
    if ($p -and -not $p.HasExited) {
      try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
  }
}

try {
  Write-Host "==> Starting backend (Flask) on http://127.0.0.1:5000" -ForegroundColor Cyan
  $backend = Start-Process -FilePath $venvPython -ArgumentList '-m', 'backend.app' `
    -WorkingDirectory $root -PassThru -NoNewWindow

  Write-Host "==> Starting frontend (Vite) on http://127.0.0.1:5173" -ForegroundColor Cyan
  $npmCmd = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { 'npm.cmd' } else { 'npm' }
  $frontend = Start-Process -FilePath $npmCmd -ArgumentList 'run', 'dev' `
    -WorkingDirectory (Join-Path $root 'frontend') -PassThru -NoNewWindow

  # Give Vite a moment to bind, then open the browser.
  Start-Sleep -Seconds 3
  Write-Host "==> Opening http://127.0.0.1:5173" -ForegroundColor Green
  Start-Process 'http://127.0.0.1:5173'

  Write-Host "`nBoth servers are running. Press Ctrl+C to stop." -ForegroundColor Yellow
  while (-not $backend.HasExited -and -not $frontend.HasExited) {
    Start-Sleep -Seconds 1
  }
} finally {
  & $cleanup
  Write-Host "`nStopped." -ForegroundColor Yellow
}
