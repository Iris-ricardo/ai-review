param(
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $projectRoot "backend"
$frontendDir = Join-Path $projectRoot "frontend"
$venvDir = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$requirements = Join-Path $backendDir "requirements.txt"
$envTemplate = Join-Path $projectRoot ".env.example"
$backendEnv = Join-Path $backendDir ".env"
$bundledNodeDir = Join-Path $projectRoot ".tools\node"
$bundledNpm = Join-Path $bundledNodeDir "npm.cmd"
$distIndex = Join-Path $frontendDir "dist\index.html"

function Test-PythonCandidate([string]$File, [string[]]$PrefixArgs) {
    try {
        & $File @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Resolve-Python {
    $candidates = @(
        [pscustomobject]@{ File = "py"; Args = @("-3.13") },
        [pscustomobject]@{ File = "py"; Args = @("-3.12") },
        [pscustomobject]@{ File = "py"; Args = @("-3.11") },
        [pscustomobject]@{ File = "py"; Args = @("-3") },
        [pscustomobject]@{ File = "python"; Args = @() }
    )
    foreach ($candidate in $candidates) {
        if (Test-PythonCandidate $candidate.File $candidate.Args) {
            return $candidate
        }
    }
    return $null
}

function Resolve-Npm {
    if (Test-Path -LiteralPath $bundledNpm) {
        return [pscustomobject]@{ Npm = $bundledNpm; NodeDir = $bundledNodeDir }
    }
    foreach ($name in @("npm.cmd", "npm")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            return [pscustomobject]@{ Npm = $command.Source; NodeDir = Split-Path -Parent $command.Source }
        }
    }
    return $null
}

function Set-EnvLine([string]$Path, [string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path -LiteralPath $Path) {
        $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
    }
    $updated = $false
    $newLines = foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Key))=") {
            "$Key=$Value"
            $updated = $true
        }
        else {
            $line
        }
    }
    if (-not $updated) {
        $newLines += "$Key=$Value"
    }
    Set-Content -LiteralPath $Path -Value $newLines -Encoding UTF8
}

function Get-EnvValue([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $line = Get-Content -LiteralPath $Path -Encoding UTF8 |
        Where-Object { $_ -match "^$([regex]::Escape($Key))=" } |
        Select-Object -First 1
    if ($null -eq $line) {
        return ""
    }
    return $line.Substring($line.IndexOf("=") + 1).Trim()
}

function Test-EnvKey([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $line = Get-Content -LiteralPath $Path -Encoding UTF8 |
        Where-Object { $_ -match "^$([regex]::Escape($Key))=" } |
        Select-Object -First 1
    return ($null -ne $line)
}

# R13: fill in missing security/runtime keys. Only whole missing lines are appended;
# user-provided values are never overwritten (including an intentionally empty
# AUTH_BOOTSTRAP_ADMIN_PASSWORD).
function Initialize-Setting([string]$Path, [string]$Key, [string]$Value) {
    if (-not (Test-EnvKey $Path $Key)) {
        Set-EnvLine $Path $Key $Value
        Write-Host "Added $Key=$Value to backend\.env (missing key, production-safe default)." -ForegroundColor DarkGray
    }
}

function New-SecureToken([int]$ByteLength = 32) {
    $bytes = New-Object byte[] $ByteLength
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Initialize-Secret(
    [string]$Path,
    [string]$Key,
    [string]$Placeholder
) {
    $current = Get-EnvValue $Path $Key
    if ([string]::IsNullOrWhiteSpace($current) -or $current -eq $Placeholder) {
        Set-EnvLine $Path $Key (New-SecureToken)
        Write-Host "Generated $Key in backend\.env." -ForegroundColor Green
    }
}

Write-Host "[1/4] Preparing backend environment..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Resolve-Python
    if ($null -eq $python) {
        throw "Python 3.11+ was not found. Install Python 3.11 or newer, then run this script again."
    }
    & $python.File @($python.Args) -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Python virtual environment." }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Failed to install backend dependencies." }

Write-Host "[2/4] Preparing backend .env..." -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $backendEnv)) {
    Copy-Item -LiteralPath $envTemplate -Destination $backendEnv
    Write-Host "Created backend\.env from .env.example (production-safe defaults). Fill LLM_API_KEY and set AI_EGRESS_ENABLED=true before using AI checks." -ForegroundColor Yellow
}

Initialize-Secret $backendEnv "ACCESS_TOKEN" "replace-with-a-random-access-token"
Initialize-Secret $backendEnv "ADMIN_TOKEN" "replace-with-a-separate-admin-token"
Initialize-Secret $backendEnv "AUTH_SECRET" "replace-with-a-random-session-signing-secret"

# 安全/运行配置的生产安全默认值，只补缺失键、不覆盖已有值（与 docker-compose.yml 默认值一致）：
# AUTH_CREATE_DEMO_USERS=false 不建演示账号；AUTH_FORCE_DEMO_PASSWORD_CHANGE=true 首登强制改密；
# AI_EGRESS_ENABLED=false 禁止材料正文离开本机。
$securityDefaults = [ordered]@{
    AUTH_REQUIRED                   = "true"
    AUTH_CREATE_DEMO_USERS          = "false"
    AUTH_FORCE_DEMO_PASSWORD_CHANGE = "true"
    AUTH_BOOTSTRAP_ADMIN_USERNAME   = "admin"
    AUTH_BOOTSTRAP_ADMIN_PASSWORD   = ""
    AI_EGRESS_ENABLED               = "false"
    MAX_UPLOAD_SIZE_MB              = "50"
    UPLOAD_DIR                      = "./uploads"
    OUTPUT_DIR                      = "./outputs"
    TESSDATA_PREFIX                 = ""
    REPORT_FONT_PATH                = ""
}
foreach ($settingKey in $securityDefaults.Keys) {
    Initialize-Setting $backendEnv $settingKey $securityDefaults[$settingKey]
}

# With production-safe defaults no demo accounts are created, so an empty database with no
# bootstrap password would leave nobody able to log in. Generate a random initial super-admin
# password (written to backend\.env only; this script never echoes the password itself).
$demoUsersSetting = Get-EnvValue $backendEnv "AUTH_CREATE_DEMO_USERS"
$bootstrapPassword = Get-EnvValue $backendEnv "AUTH_BOOTSTRAP_ADMIN_PASSWORD"
$bootstrapUsername = Get-EnvValue $backendEnv "AUTH_BOOTSTRAP_ADMIN_USERNAME"
if ($bootstrapUsername -eq "") { $bootstrapUsername = "admin" }
$generatedBootstrapPassword = $false
if ($demoUsersSetting -notmatch "^(?i:true|1)$" -and [string]::IsNullOrWhiteSpace($bootstrapPassword)) {
    Set-EnvLine $backendEnv "AUTH_BOOTSTRAP_ADMIN_PASSWORD" (New-SecureToken 24)
    $generatedBootstrapPassword = $true
    Write-Host "Generated AUTH_BOOTSTRAP_ADMIN_PASSWORD in backend\.env (value is not printed by this script)." -ForegroundColor Green
}

$soffice = Join-Path $projectRoot ".tools\LibreOffice\program\soffice.exe"
if (Test-Path -LiteralPath $soffice) {
    $currentSoffice = ""
    $line = Get-Content -LiteralPath $backendEnv -Encoding UTF8 | Where-Object { $_ -match "^SOFFICE_PATH=" } | Select-Object -First 1
    if ($null -ne $line) {
        $currentSoffice = $line.Substring("SOFFICE_PATH=".Length).Trim()
    }
    if ([string]::IsNullOrWhiteSpace($currentSoffice)) {
        Set-EnvLine $backendEnv "SOFFICE_PATH" $soffice
    }
}

if (-not $SkipFrontend) {
    Write-Host "[3/4] Preparing frontend assets..." -ForegroundColor Cyan
    $npmTool = Resolve-Npm
    if ($null -eq $npmTool) {
        if (Test-Path -LiteralPath $distIndex) {
            Write-Warning "Node/npm was not found. Existing frontend\dist will be used, but frontend changes cannot be rebuilt here."
        }
        else {
            throw "Node/npm was not found and frontend\dist is missing. Install Node.js or copy the packaged .tools directory, then run this script again."
        }
    }
    else {
        Push-Location $frontendDir
        $oldPath = $env:Path
        try {
            if (Test-Path -LiteralPath $npmTool.NodeDir) {
                $env:Path = "$($npmTool.NodeDir);$oldPath"
            }
            & $npmTool.Npm ci
            if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }
            & $npmTool.Npm run build
            if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
        }
        finally {
            $env:Path = $oldPath
            Pop-Location
        }
    }
}
else {
    Write-Host "[3/4] Skipped frontend setup." -ForegroundColor Yellow
}

Write-Host "[4/4] Setup complete." -ForegroundColor Green
Write-Host ""
Write-Host "Security defaults applied to backend\.env (production-safe):" -ForegroundColor Cyan
Write-Host "  AUTH_REQUIRED=true / AUTH_CREATE_DEMO_USERS=false / AUTH_FORCE_DEMO_PASSWORD_CHANGE=true / AI_EGRESS_ENABLED=false"
Write-Host "  First login account: $bootstrapUsername"
if ($generatedBootstrapPassword) {
    Write-Host "  Its initial password was randomly generated into backend\.env (AUTH_BOOTSTRAP_ADMIN_PASSWORD)." -ForegroundColor Yellow
    Write-Host "  Read it from backend\.env yourself; this script never prints passwords. You must change it at first login." -ForegroundColor Yellow
}
else {
    Write-Host "  Initial admin password: see AUTH_BOOTSTRAP_ADMIN_PASSWORD in backend\.env." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Demo / competition demo environment (must be enabled explicitly):" -ForegroundColor Cyan
Write-Host "  * Demo accounts (user/maintainer/reviewer/admin/superadmin, fixed passwords):"
Write-Host "      set AUTH_CREATE_DEMO_USERS=true in backend\.env (only takes effect on a first start with an empty database)."
Write-Host "      Only for offline local demos: those passwords are derivable from the source and documentation."
Write-Host "  * Skip forced first-login password change: set AUTH_FORCE_DEMO_PASSWORD_CHANGE=false (only with demo accounts)."
Write-Host "  * AI semantic review: set AI_EGRESS_ENABLED=true and fill LLM_API_KEY,"
Write-Host "      after confirming the uploaded materials are authorised for external transmission."
Write-Host ""
Write-Host "Docker deployment uses a different template: copy .env.docker.example to .env (container paths) and run docker compose up --build -d."
Write-Host "Next: double-click 一键启动项目.bat or run .\launch_project.ps1"
