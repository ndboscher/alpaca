# Run any zdte command inside the project's virtualenv, e.g. .\run.ps1 run --dry-run
Set-Location $PSScriptRoot
if (-not (Test-Path .\.venv\Scripts\zdte.exe)) {
    Write-Error "Not set up yet - run .\setup.ps1 first."
    exit 1
}
if ($args.Count -eq 0) { $args = @("run") }
& .\.venv\Scripts\zdte.exe @args
exit $LASTEXITCODE
