param(
    [Parameter(Mandatory = $true)][string]$InstallDir
)
$ErrorActionPreference = "Stop"
$python = Join-Path $InstallDir "runtime\python\python.exe"
$envFile = Join-Path $InstallDir ".env"
$hardwareFile = Join-Path $InstallDir "hardware.json"

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $lines = if (Test-Path $envFile) { @(Get-Content -LiteralPath $envFile) } else { @() }
    $found = $false
    $out = foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($Name))=") {
            $found = $true
            "$Name=$Value"
        } else {
            $line
        }
    }
    if (-not $found) { $out = @($out) + "$Name=$Value" }
    $out | Set-Content -LiteralPath $envFile -Encoding utf8
}

function Find-Ollama {
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        (Join-Path $env:ProgramFiles "Ollama\ollama.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Show-Hardware {
    if (-not (Test-Path $hardwareFile)) {
        Write-Host "Профиль железа не найден. Запустите установщик ещё раз." -ForegroundColor Yellow
        return
    }
    $data = Get-Content -LiteralPath $hardwareFile -Raw -Encoding utf8 | ConvertFrom-Json
    Write-Host ""
    Write-Host "Железо:" -ForegroundColor Cyan
    Write-Host ("  RAM: {0} ГБ, ядер: {1}, свободно: {2} ГБ" -f `
        $data.profile.ram_gb, $data.profile.cpu_cores, $data.profile.disk_free_gb)
    Write-Host ("  GPU: {0}, VRAM: {1} ГБ" -f `
        ($(if ($data.profile.gpu_name) { $data.profile.gpu_name } else { "не обнаружен" })), `
        $data.profile.vram_gb)
    Write-Host ""
    Write-Host "Рекомендации:" -ForegroundColor Cyan
    Write-Host ("  Быстро:      {0}" -f $data.recommendation.fast_model)
    Write-Host ("  Качественно: {0}" -f $data.recommendation.quality_model)
    Write-Host ("  API:         {0}" -f $data.recommendation.api_model)
    foreach ($note in $data.recommendation.notes) {
        Write-Host ("  - {0}" -f $note)
    }
    Write-Host ""
}

function Ensure-Ollama {
    $ollama = Find-Ollama
    if ($ollama) { return $ollama }
    $setup = Join-Path $InstallDir "vendor\OllamaSetup.exe"
    if (Test-Path $setup) {
        Write-Host "Устанавливаю Ollama из комплекта..." -ForegroundColor Yellow
        Start-Process -FilePath $setup -Wait
        $ollama = Find-Ollama
        if ($ollama) { return $ollama }
    }
    Write-Host "Ollama не найдена." -ForegroundColor Yellow
    Write-Host "Скачайте вручную: https://ollama.com/download/windows"
    Write-Host "Или положите OllamaSetup.exe в папку vendor установщика."
    return $null
}

function Pull-Model {
    param([string]$Ollama, [string]$Model)
    Write-Host "Скачиваю модель $Model ..." -ForegroundColor Yellow
    & $Ollama pull $Model
    if ($LASTEXITCODE -ne 0) { throw "ollama pull $Model завершился с кодом $LASTEXITCODE" }
    Set-EnvValue "LLM_PROVIDER" "ollama"
    Set-EnvValue "OLLAMA_ENABLED" "true"
    Set-EnvValue "OLLAMA_MODEL" $Model
}

function Import-Model {
    param([string]$Ollama)
    $folder = Read-Host "Путь к папке или флешке с .gguf"
    if (-not (Test-Path $folder)) { throw "Папка не найдена: $folder" }
    $gguf = Get-ChildItem -LiteralPath $folder -Filter *.gguf -File |
        Select-Object -First 1
    if (-not $gguf) { throw "В папке нет .gguf" }

    $models = Join-Path $InstallDir "models"
    New-Item -ItemType Directory -Force $models | Out-Null
    $target = Join-Path $models $gguf.Name
    Copy-Item -LiteralPath $gguf.FullName -Destination $target -Force

    $modelfile = Join-Path $models "Modelfile"
    @"
FROM ./$($gguf.Name)
PARAMETER num_ctx 4096
PARAMETER temperature 0.1
"@ | Set-Content -LiteralPath $modelfile -Encoding utf8

    Push-Location $models
    try {
        & $Ollama create "courtdraft-quality" -f "Modelfile"
        if ($LASTEXITCODE -ne 0) { throw "ollama create завершился с кодом $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
    Set-EnvValue "LLM_PROVIDER" "ollama"
    Set-EnvValue "OLLAMA_ENABLED" "true"
    Set-EnvValue "OLLAMA_MODEL" "courtdraft-quality"
}

function Setup-Api {
    $keyPath = Read-Host "Путь к файлу с ключом ZvenoAI (ключ не печатается)"
    if (-not (Test-Path $keyPath)) { throw "Файл ключа не найден" }
    & $python -X utf8 (Join-Path $InstallDir "scripts\set_openai_key.py") `
        --key-file $keyPath --env-file $envFile
    if ($LASTEXITCODE -ne 0) { throw "set_openai_key.py завершился с кодом $LASTEXITCODE" }
}

function Restart-App {
    $svc = Get-Service -Name "CourtDraftAI" -ErrorAction SilentlyContinue
    if ($svc) {
        Restart-Service -Name "CourtDraftAI" -Force
        Write-Host "Служба перезапущена: http://127.0.0.1:8081/app" -ForegroundColor Green
        return
    }
    Write-Host "Запустите приложение: runtime\start-portable.cmd" -ForegroundColor Green
    Write-Host "Адрес: http://127.0.0.1:8080/app"
}

Show-Hardware

Write-Host "Что установить?" -ForegroundColor Cyan
Write-Host "  1. Быстрая модель (1.5B, работает почти везде)"
Write-Host "  2. Качественная модель (по профилю железа)"
Write-Host "  3. Импорт GGUF с флешки"
Write-Host "  4. API DeepSeek Flash (ZvenoAI, нужен ключ)"
Write-Host "  5. Позже"
$choice = Read-Host "Выбор [1-5]"

if ($choice -in @("1", "2")) {
    $ollama = Ensure-Ollama
    if (-not $ollama) { exit 2 }
    if ($choice -eq "1") {
        $model = "qwen2.5:1.5b"
    } else {
        if (Test-Path $hardwareFile) {
            $data = Get-Content -LiteralPath $hardwareFile -Raw -Encoding utf8 | ConvertFrom-Json
            $model = $data.recommendation.quality_model
        } else {
            $model = "qwen2.5:7b"
        }
    }
    Pull-Model -Ollama $ollama -Model $model
    Restart-App
    exit 0
}

if ($choice -eq "3") {
    $ollama = Ensure-Ollama
    if (-not $ollama) { exit 2 }
    Import-Model -Ollama $ollama
    Restart-App
    exit 0
}

if ($choice -eq "4") {
    Setup-Api
    Restart-App
    exit 0
}

Write-Host "Настройка отложена. Приложение будет работать в MOCK-режиме." `
    -ForegroundColor Yellow
