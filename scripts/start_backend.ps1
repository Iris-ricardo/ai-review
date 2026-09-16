$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$backend = Join-Path $projectRoot "backend"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}

Push-Location $backend
try {
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
}
finally {
    Pop-Location
}
