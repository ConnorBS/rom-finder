<#
.SYNOPSIS
  Launch rom-finder locally from the test venv on a throwaway DB, wait for it to
  answer, and print /api/status. For autonomous verification of a change without
  prod and without a human opening the site. Needs the test venv at ./venv
  (python -m venv venv; venv\Scripts\pip install -r requirements.txt).

.EXAMPLE
  # One-shot: boot, print status JSON, tear down
  pwsh scripts/run-local.ps1

.EXAMPLE
  # Keep it running (e.g. to curl more endpoints by hand)
  pwsh scripts/run-local.ps1 -KeepRunning
#>
param(
  [int]$Port = 19847,
  [switch]$KeepRunning
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$tmpDb = Join-Path $env:TEMP ("rf_local_{0}.db" -f $Port)
Remove-Item "$tmpDb*" -ErrorAction SilentlyContinue
$env:DB_URL = "sqlite:///" + ($tmpDb -replace '\\', '/')
$env:APP_VERSION = "local"

$proc = Start-Process -FilePath $py `
  -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port") `
  -WorkingDirectory $root -PassThru -NoNewWindow
try {
  $url = "http://127.0.0.1:$Port/api/status"
  $ready = $false
  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try { $resp = Invoke-RestMethod -Uri $url -TimeoutSec 2; $ready = $true; break } catch {}
  }
  if (-not $ready) { throw "rom-finder did not become ready at $url" }
  $resp | ConvertTo-Json -Depth 8
  if ($KeepRunning) {
    Write-Host "`nRunning at http://127.0.0.1:$Port (PID $($proc.Id)). Ctrl+C to stop."
    Wait-Process -Id $proc.Id
  }
}
finally {
  if (-not $KeepRunning) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item "$tmpDb*" -ErrorAction SilentlyContinue
  }
}
