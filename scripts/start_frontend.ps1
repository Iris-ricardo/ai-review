$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$nodeDir = Join-Path $projectRoot ".tools\node"
$npm = Join-Path $nodeDir "npm.cmd"
$frontend = Join-Path $projectRoot "frontend"

if (-not (Test-Path -LiteralPath $npm)) {
    throw "Project-local npm not found: $npm"
}

$env:Path = "$nodeDir;$env:Path"
Push-Location $frontend
try {
    & $npm run dev
}
finally {
    Pop-Location
}
