param(
    [switch]$IncludeRuntime,
    [switch]$IncludeData,
    [string]$OutputDir
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $projectRoot "release"
}

$configFile = Join-Path $projectRoot "backend\app\core\config.py"
$version = "local"
if (Test-Path -LiteralPath $configFile) {
    $versionLine = Get-Content -LiteralPath $configFile -Encoding UTF8 |
        Where-Object { $_ -match 'APP_VERSION:\s*str\s*=\s*"([^"]+)"' } |
        Select-Object -First 1
    if ($versionLine -match '"([^"]+)"') {
        $version = $Matches[1]
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$packageName = "qingyuanbei-ai-review-v$version-$stamp"
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) $packageName
$stageRoot = Join-Path $tempRoot $packageName

if (Test-Path -LiteralPath $tempRoot) {
    $resolvedTemp = (Resolve-Path -LiteralPath $tempRoot).Path
    $systemTemp = [System.IO.Path]::GetTempPath()
    if (-not $resolvedTemp.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a staging directory outside the system temp folder: $resolvedTemp"
    }
    Remove-Item -LiteralPath $tempRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

function Test-Excluded([string]$RelativePath) {
    $rel = $RelativePath.Replace("/", "\")
    $lower = $rel.ToLowerInvariant()

    $blockedPrefixes = @(
        ".git\",
        ".idea\",
        ".vscode\",
        ".claude\",
        ".agents\",
        ".codex\",
        ".run\",
        ".pytest-tmp\",
        ".pnpm-store\",
        "release\",
        "frontend\node_modules\",
        "backend\.pytest_cache\",
        "backend\.pytest-tmp\",
        "rules\.history\"
    )
    if (-not $IncludeRuntime) {
        $blockedPrefixes += ".tools\"
    }
    if (-not $IncludeData) {
        $blockedPrefixes += "backend\uploads\"
        $blockedPrefixes += "backend\outputs\"
    }

    foreach ($prefix in $blockedPrefixes) {
        if ($lower.StartsWith($prefix)) { return $true }
    }

    if ($lower -eq "backend\.env") { return $true }
    # R13：根目录 .env 是 docker compose 的插值文件，含真实密钥，默认不打包。
    # 模板 .env.example / .env.docker.example 不含密钥，照常打包。
    if ($lower -eq ".env") { return $true }
    if ($lower -eq "package_project.ps1") { return $false }
    if ($lower -like "*.pyc" -or $lower -like "*.pyo") { return $true }
    if ($lower.Contains("\__pycache__\")) { return $true }
    if ($lower -like "*.log") { return $true }
    if (-not $IncludeData -and $lower -like "backend\review.db*") { return $true }
    if (-not $IncludeRuntime -and $lower.StartsWith(".venv\")) { return $true }
    if ($lower.StartsWith(".venv\")) { return $true }

    return $false
}

Write-Host "Packaging project..." -ForegroundColor Cyan
Get-ChildItem -LiteralPath $projectRoot -File -Recurse -Force | ForEach-Object {
    $relative = $_.FullName.Substring($projectRoot.Length).TrimStart("\")
    if (-not (Test-Excluded $relative)) {
        $target = Join-Path $stageRoot $relative
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target
    }
}

$manifest = @(
    "Package: $packageName",
    "CreatedAt: $(Get-Date -Format o)",
    "IncludeRuntime: $IncludeRuntime",
    "IncludeData: $IncludeData",
    "",
    "Excluded by default:",
    "- backend/.env, .env (docker compose) and all local secrets",
    "- backend/review.db*, uploads, outputs unless -IncludeData is used",
    "- .venv, node_modules, caches, logs, git metadata",
    "",
    "First run on another Windows computer:",
    "1. powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1",
    "2. edit backend\.env: fill LLM_API_KEY and set AI_EGRESS_ENABLED=true if AI checks are needed",
    "3. double-click 一键启动项目.bat",
    "",
    "Docker: copy .env.docker.example to .env (container paths), then docker compose up --build -d"
)
Set-Content -LiteralPath (Join-Path $stageRoot "PACKAGE_MANIFEST.txt") -Value $manifest -Encoding UTF8

$zipPath = Join-Path $OutputDir "$packageName.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue | Select-Object -First 1
$created = $false
# tar.exe(bsdtar) 处理不了非 ASCII 输出路径：中文会被转成 ? 并以 “Failed to open” 失败，
# 因此路径含非 ASCII 字符时不用 tar；tar 运行失败也回退，不直接抛错。
$asciiPath = $zipPath -match '^[\x00-\x7F]+$'
if ($null -ne $tar -and $asciiPath) {
    Push-Location $tempRoot
    try {
        & $tar.Source -a -cf $zipPath $packageName
        if ($LASTEXITCODE -eq 0) { $created = $true }
        else { Write-Host "tar.exe failed (exit $LASTEXITCODE); falling back to the built-in zip writer." -ForegroundColor Yellow }
    }
    finally {
        Pop-Location
    }
}
if (-not $created) {
    # Compress-Archive 与 .NET CreateFromDirectory 在 Windows PowerShell 5.1 下会写入反斜杠条目名
    # （不符合 ZIP 规范，macOS/Linux 解压会得到带反斜杠的怪文件名），因此手工建条目并统一用正斜杠。
    Add-Type -AssemblyName System.IO.Compression | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
    $archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -LiteralPath $stageRoot -Recurse -File -Force | ForEach-Object {
            $entryName = $_.FullName.Substring($tempRoot.Length).TrimStart('\').Replace('\', '/')
            $entry = $archive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            $target = $entry.Open()
            $source = [System.IO.File]::OpenRead($_.FullName)
            try { $source.CopyTo($target) }
            finally { $source.Dispose(); $target.Dispose() }
        }
    }
    finally { $archive.Dispose() }
}
Remove-Item -LiteralPath $tempRoot -Recurse -Force

Write-Host "Package created: $zipPath" -ForegroundColor Green
if (-not $IncludeRuntime) {
    Write-Host "Runtime tools were not included. Use -IncludeRuntime to include .tools/node and .tools/LibreOffice." -ForegroundColor Yellow
}
if (-not $IncludeData) {
    Write-Host "Local review database, uploads, and outputs were not included." -ForegroundColor Yellow
}
