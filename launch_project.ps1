param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $projectRoot "backend"
$frontendDir = Join-Path $projectRoot "frontend"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$bundledNodeDir = Join-Path $projectRoot ".tools\node"
$bundledNodeExe = Join-Path $bundledNodeDir "node.exe"
$bundledNpm = Join-Path $bundledNodeDir "npm.cmd"
$distIndex = Join-Path $frontendDir "dist\index.html"
$runDir = Join-Path $projectRoot ".run"
$pidFile = Join-Path $runDir "backend.pid"
$runtimeLog = Join-Path $backendDir "server.runtime.log"
$healthUrl = "http://127.0.0.1:8000/api/health"
$appUrl = "http://127.0.0.1:8000"

# Some automation hosts inject both Path and PATH. PowerShell 5 refuses any
# Start-Process call (including opening the browser) until the duplicate is gone.
$environmentKeys = @([Environment]::GetEnvironmentVariables("Process").Keys)
$hasUpperPath = @($environmentKeys | Where-Object { $_ -ceq "PATH" }).Count -gt 0
$hasTitlePath = @($environmentKeys | Where-Object { $_ -ceq "Path" }).Count -gt 0
if ($hasUpperPath -and $hasTitlePath) {
    [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
}

function Get-Health {
    try {
        return Invoke-RestMethod $healthUrl -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Wait-Health([string]$ExpectedBuild) {
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        $health = Get-Health
        if ($null -ne $health -and $health.build_id -eq $ExpectedBuild) {
            return $health
        }
        Start-Sleep -Seconds 1
    }
    throw "Backend was not ready in 60 seconds. See $runtimeLog"
}

function Get-PortProcess {
    $connection = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $connection) { return $null }
    return Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
}

function Resolve-Tool([string[]]$Names) {
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python environment is missing. Run setup_windows.ps1 first."
}

New-Item -ItemType Directory -Path $runDir -Force | Out-Null

Push-Location $backendDir
try {
    $identityLines = & $python -c "from app.core.config import PROJECT_INSTANCE_FINGERPRINT,SOURCE_FINGERPRINT,get_settings; print(get_settings().APP_BUILD_ID); print(PROJECT_INSTANCE_FINGERPRINT); print(SOURCE_FINGERPRINT)"
    $expectedBuild = $identityLines[0].Trim()
    $expectedInstance = $identityLines[1].Trim()
    $expectedSource = $identityLines[2].Trim()
}
finally {
    Pop-Location
}

$sourceFiles = Get-ChildItem -LiteralPath (Join-Path $frontendDir "src") -File -Recurse
$sourceFiles += Get-Item (Join-Path $frontendDir "package.json"), (Join-Path $frontendDir "package-lock.json"), (Join-Path $frontendDir "index.html")
$latestSource = ($sourceFiles | Measure-Object LastWriteTimeUtc -Maximum).Maximum
$needsBuild = -not (Test-Path -LiteralPath $distIndex)
if (-not $needsBuild) {
    $needsBuild = (Get-Item -LiteralPath $distIndex).LastWriteTimeUtc -lt $latestSource
}
if ($needsBuild) {
    $nodeDir = $bundledNodeDir
    $npm = $bundledNpm
    if (-not (Test-Path -LiteralPath $bundledNodeExe) -or -not (Test-Path -LiteralPath $bundledNpm)) {
        $npm = Resolve-Tool @("npm.cmd", "npm")
        if ($null -eq $npm) {
            throw "Frontend assets need to be rebuilt, but Node/npm is missing. Install Node.js or rerun setup_windows.ps1 on the target computer."
        }
        $node = Resolve-Tool @("node.exe", "node")
        if ($null -ne $node) {
            $nodeDir = Split-Path -Parent $node
        }
    }
    Write-Host "[Frontend] Building production assets..." -ForegroundColor Cyan
    Push-Location $frontendDir
    $originalProcessPath = $env:Path
    try {
        if (Test-Path -LiteralPath $nodeDir) {
            $env:Path = "$nodeDir;$originalProcessPath"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $frontendDir "node_modules"))) {
            & $npm ci
            if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }
        }
        & $npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
    }
    finally {
        $env:Path = $originalProcessPath
        Pop-Location
    }
}

$health = Get-Health
if ($null -ne $health -and $health.build_id -eq $expectedBuild -and $health.instance_fingerprint -eq $expectedInstance -and $health.source_fingerprint -eq $expectedSource) {
    Write-Host "[Backend] Current build is already running." -ForegroundColor Green
    @{
        service_pid = [int]$health.process_id
        launcher_pid = 0
        build_id = $expectedBuild
        instance_fingerprint = $expectedInstance
        source_fingerprint = $expectedSource
    } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding ascii
}
elseif ($null -ne $health) {
    if ([int]$health.checks.active_tasks -gt 0) {
        throw "An older build on port 8000 still has active tasks. Wait or run stop_project.ps1 explicitly."
    }
    if ($health.instance_fingerprint -ne $expectedInstance) {
        throw "Port 8000 is owned by another program and cannot be replaced safely."
    }
    Stop-Process -Id ([int]$health.process_id) -Force
    if (Test-Path -LiteralPath $pidFile) {
        try {
            $previousRecord = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
            $previousLauncher = Get-Process -Id ([int]$previousRecord.launcher_pid) -ErrorAction SilentlyContinue
            if ($null -ne $previousLauncher -and $previousLauncher.Path.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $previousLauncher.Id -Force
            }
        }
        catch {}
    }
    Start-Sleep -Milliseconds 500
    $health = $null
}
elseif ($null -ne (Get-PortProcess)) {
    throw "Port 8000 is occupied by a non-project service."
}

if ($null -eq $health) {
    Write-Host "[Backend] Starting..." -ForegroundColor Cyan
    # Start-Process keeps the service alive after this launcher exits.
    $process = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1") `
        -WorkingDirectory $backendDir `
        -WindowStyle Hidden `
        -PassThru
    $health = Wait-Health $expectedBuild
    if ($health.instance_fingerprint -ne $expectedInstance -or $health.source_fingerprint -ne $expectedSource) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "The started service identity does not match the current project source."
    }
    @{
        service_pid = [int]$health.process_id
        launcher_pid = [int]$process.Id
        build_id = $expectedBuild
        instance_fingerprint = $expectedInstance
        source_fingerprint = $expectedSource
    } | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding ascii
}

Write-Host "Ready: $appUrl" -ForegroundColor Green
Write-Host "Version: $($health.version) / $($health.build_id)"
if ($health.status -ne "ok") {
    Write-Warning "Service is degraded. Inspect $healthUrl"
}
if (-not $NoBrowser) {
    Start-Process $appUrl
}
