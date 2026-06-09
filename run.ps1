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

function Stop-PortProcess($port) {
  try {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
  } catch {
    $connections = @()
  }
  $pids = @($connections | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -and $_ -ne $PID })
  foreach ($processId in $pids) {
    Write-Host "  freeing port ${port}: stopping PID ${processId}" -ForegroundColor Yellow
    try { taskkill /PID $processId /T /F 2>$null | Out-Null } catch {}
  }
}

function Wait-Http($url, $expectedText = $null, $maxWait = 30) {
  for ($i = 0; $i -lt $maxWait; $i++) {
    Start-Sleep -Seconds 1
    try {
      $response = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing
      if (-not $expectedText -or $response.Content.Contains($expectedText)) {
        return $true
      }
    } catch {}
  }
  return $false
}

# Stop child processes when this script exits (Ctrl+C or normal end).
# npm.cmd is a wrapper whose PID is the shim, not the node/vite worker it spawns,
# so use taskkill /T to terminate the whole process tree (frees port 5173).
$cleanup = {
  foreach ($p in @($script:backend, $script:frontend)) {
    if ($p -and -not $p.HasExited) {
      try { taskkill /PID $p.Id /T /F 2>$null | Out-Null } catch {}
    }
  }
}

try {
  Write-Host "==> Freeing old dev server ports (5000, 5173)" -ForegroundColor Cyan
  Stop-PortProcess 5000
  Stop-PortProcess 5173

  # Run without the debug reloader for a clean one-shot launch.
  $env:FLASK_DEBUG = '0'
  Write-Host "==> Starting backend (Flask) on http://127.0.0.1:5000" -ForegroundColor Cyan
  $backend = Start-Process -FilePath $venvPython -ArgumentList '-m', 'backend.app' `
    -WorkingDirectory $root -PassThru -NoNewWindow

  Write-Host "==> Waiting for backend to be ready on http://127.0.0.1:5000" -ForegroundColor Cyan
  if (-not (Wait-Http 'http://127.0.0.1:5000/api/health' 'img-Creater-backend' 30)) {
    Write-Host "[ERROR] Backend did not start with the current img-Creater code." -ForegroundColor Red
    exit 1
  }

  Write-Host "==> Starting frontend (Vite) on http://127.0.0.1:5173" -ForegroundColor Cyan
  $npmCmd = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { 'npm.cmd' } else { 'npm' }
  $frontend = Start-Process -FilePath $npmCmd -ArgumentList 'run', 'dev' `
    -WorkingDirectory (Join-Path $root 'frontend') -PassThru -NoNewWindow

  Write-Host "==> Waiting for Vite to be ready on http://127.0.0.1:5173" -ForegroundColor Cyan
  if (-not (Wait-Http 'http://127.0.0.1:5173' $null 30)) {
    Write-Host "  Vite didn't respond in 30s - opening browser anyway" -ForegroundColor Yellow
  }
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
