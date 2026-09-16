$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $projectRoot "backend"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pidFile = Join-Path $projectRoot ".run\backend.pid"
$healthUrl = "http://127.0.0.1:8000/api/health"

if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host "No recorded project process is running."
    exit 0
}

Push-Location $backendDir
try {
    $identityLines = & $python -c "from app.core.config import PROJECT_INSTANCE_FINGERPRINT,get_settings; print(get_settings().APP_BUILD_ID); print(PROJECT_INSTANCE_FINGERPRINT)"
    $expectedBuild = $identityLines[0].Trim()
    $expectedInstance = $identityLines[1].Trim()
}
finally {
    Pop-Location
}

$rawRecord = Get-Content -LiteralPath $pidFile -Raw
try {
    $record = $rawRecord | ConvertFrom-Json
    $servicePid = [int]$record.service_pid
    $launcherPid = [int]$record.launcher_pid
}
catch {
    $servicePid = [int]$rawRecord
    $launcherPid = $servicePid
}

$health = $null
try { $health = Invoke-RestMethod $healthUrl -TimeoutSec 2 } catch {}
if ($null -ne $health) {
    if ([int]$health.process_id -ne $servicePid -or $health.build_id -ne $expectedBuild -or $health.instance_fingerprint -ne $expectedInstance) {
        throw "The running service does not match the recorded project instance."
    }
    Stop-Process -Id $servicePid -Force
    Start-Sleep -Milliseconds 500
}
elseif ($null -ne (Get-Process -Id $servicePid -ErrorAction SilentlyContinue)) {
    throw "The recorded service PID is alive but its identity cannot be verified."
}

if ($launcherPid -gt 0 -and $launcherPid -ne $servicePid) {
    $launcher = Get-Process -Id $launcherPid -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        if (-not $launcher.Path.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "The launcher PID does not belong to this project."
        }
        Stop-Process -Id $launcherPid -Force
    }
}

Remove-Item -LiteralPath $pidFile -Force
Write-Host "Project stopped." -ForegroundColor Green
