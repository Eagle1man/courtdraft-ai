[CmdletBinding()]
param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$installer = $PSScriptRoot
$root = Split-Path -Parent $installer
$stage = Join-Path $installer "stage"
$dist = Join-Path $installer "dist"
$runtime = Join-Path $stage "runtime\python"
$site = Join-Path $runtime "Lib\site-packages"

Write-Host "== CourtDraft AI installer build ==" -ForegroundColor Cyan
Write-Host "Repo: $root"

# 1. Python 3.12 обязателен: на 3.14 колёса несовместимы с embeddable 3.12.
$py312 = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
if (-not $py312) {
    throw "Нет Python 3.12. Установите Python 3.12 x64 и повторите."
}
Write-Host "Python 3.12: $py312"

# 2. Чистим только сгенерированный stage.
if (Test-Path $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Force $stage, $dist, $runtime, $site | Out-Null

# 3. Разворачиваем embeddable Python.
$embedZip = Join-Path $installer "vendor\python-3.12.10-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    throw "Нет $embedZip. Скачайте python-3.12.10-embed-amd64.zip с python.org."
}
Expand-Archive -LiteralPath $embedZip -DestinationPath $runtime -Force

# 4. Ставим зависимости в site-packages runtime, не в системный Python.
$buildVenv = Join-Path $installer ".build-venv"
if (-not (Test-Path $buildVenv)) {
    & py -3.12 -m venv $buildVenv
}
$buildPython = Join-Path $buildVenv "Scripts\python.exe"
& $buildPython -m pip install --upgrade pip
& $buildPython -m pip install --target $site --upgrade --no-compile `
    -r (Join-Path $installer "requirements-runtime.txt")
if ($LASTEXITCODE -ne 0) {
    throw "pip install завершился с кодом $LASTEXITCODE"
}

# 5. Копируем приложение и статические файлы.
$backend = Join-Path $root "backend"
$stageBackend = Join-Path $stage "backend"
New-Item -ItemType Directory -Force $stageBackend | Out-Null
Copy-Item -LiteralPath (Join-Path $backend "app") -Destination $stageBackend -Recurse
Copy-Item -LiteralPath (Join-Path $backend "scripts") -Destination $stageBackend -Recurse
Copy-Item -LiteralPath (Join-Path $root "frontend") -Destination $stage -Recurse
Copy-Item -LiteralPath (Join-Path $root "data\templates") -Destination (Join-Path $stage "data") -Recurse

$demoStage = Join-Path $stage "data\demo"
New-Item -ItemType Directory -Force $demoStage | Out-Null
Copy-Item -Path (Join-Path $root "data\demo\*.txt") -Destination $demoStage -Force
$manifest = Join-Path $root "data\demo\manifest.json"
if (Test-Path $manifest) {
    Copy-Item -LiteralPath $manifest -Destination $demoStage -Force
}

New-Item -ItemType Directory -Force (Join-Path $stage "scripts") | Out-Null
Copy-Item -LiteralPath (Join-Path $root "scripts\hardware_profile.py") `
    -Destination (Join-Path $stage "scripts")
Copy-Item -LiteralPath (Join-Path $root "scripts\set_openai_key.py") `
    -Destination (Join-Path $stage "scripts") -ErrorAction SilentlyContinue

# 6. WinSW для службы.
$winsw = Join-Path $installer "vendor\winsw.exe"
if (-not (Test-Path $winsw)) {
    throw "Нет $winsw. Скачайте WinSW x64 с github.com/winsw/winsw/releases."
}
New-Item -ItemType Directory -Force (Join-Path $stage "vendor") | Out-Null
Copy-Item -LiteralPath $winsw -Destination (Join-Path $stage "vendor\courtdraft-service.exe")
Copy-Item -LiteralPath (Join-Path $installer "vendor\courtdraft-service.xml") `
    -Destination (Join-Path $stage "vendor")

# 7. Ollama и Tesseract — опционально, без весов.
$ollamaSetup = Join-Path $installer "vendor\OllamaSetup.exe"
if (Test-Path $ollamaSetup) {
    Copy-Item -LiteralPath $ollamaSetup -Destination (Join-Path $stage "vendor")
} else {
    Write-Warning "OllamaSetup.exe не найден: онлайн-установка попросит скачать Ollama вручную."
}

# 8. Шаблон .env и launcher-файлы.
Copy-Item -LiteralPath (Join-Path $installer "env.template") -Destination $stage
Copy-Item -LiteralPath (Join-Path $installer "runtime\*.cmd") `
    -Destination (Join-Path $stage "runtime") -Force

# 9. Защита от весов в установщике.
$weights = Get-ChildItem -LiteralPath $stage -Recurse -File `
    -Include *.gguf,*.safetensors -ErrorAction SilentlyContinue
if ($weights) {
    $names = ($weights | ForEach-Object { $_.FullName }) -join "`n"
    throw "В stage попали веса моделей:`n$names"
}

# 10. Проверка обязательных файлов.
foreach ($required in @(
    "backend\app\main.py",
    "frontend\index.html",
    "runtime\python\python.exe",
    "scripts\hardware_profile.py",
    "vendor\courtdraft-service.exe",
    ".env.template"
)) {
    $path = Join-Path $stage $required
    if (-not (Test-Path $path)) {
        throw "В stage нет обязательного файла: $required"
    }
}

# 11. Inno Setup.
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) {
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source } else { throw "Не найден ISCC.exe (Inno Setup 6)." }
}
Push-Location $installer
try {
    & $iscc "/DMyAppVersion=$Version" "courtdraft.iss"
    if ($LASTEXITCODE -ne 0) { throw "ISCC завершился с кодом $LASTEXITCODE" }
} finally {
    Pop-Location
}

$exe = Join-Path $dist "CourtDraftAI-$Version-x64.exe"
if (-not (Test-Path $exe)) { throw "Установщик не создан: $exe" }
Write-Host "OK: $exe" -ForegroundColor Green
