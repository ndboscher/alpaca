# One-time setup for Windows. In PowerShell, from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
$pyArgs = @()
foreach ($c in @("py -3", "python", "python3")) {
    $parts = $c.Split(" ")
    $exe = $parts[0]
    $pre = @($parts | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    & $exe @pre -c "import sys; sys.exit(sys.version_info < (3,10))" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $exe; $pyArgs = $pre; break }
}
if (-not $py) {
    Write-Error "Python 3.10+ is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH') and re-run."
}

if (-not (Test-Path .venv)) { & $py @pyArgs -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed" }

if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env  -> add your Alpaca API keys" }
if (-not (Test-Path config.yaml)) { Copy-Item config.example.yaml config.yaml; Write-Host "Created config.yaml -> adjust strategy/risk settings" }

Write-Host ""
Write-Host "Setup complete."
Write-Host "  1. Edit .env and paste your Alpaca PAPER API key and secret."
Write-Host "  2. .\run.ps1 check          # verify keys + options approval"
Write-Host "  3. .\run.ps1 run --dry-run  # watch signals without placing orders"
Write-Host "  4. .\run.ps1 run            # trade on your paper account"
