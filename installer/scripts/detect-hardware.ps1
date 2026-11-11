param(
    [Parameter(Mandatory = $true)][string]$InstallDir
)
$ErrorActionPreference = "Stop"
$python = Join-Path $InstallDir "runtime\python\python.exe"
$script = Join-Path $InstallDir "scripts\hardware_profile.py"
$out = Join-Path $InstallDir "hardware.json"
if (-not (Test-Path $python)) { throw "Нет runtime Python: $python" }
if (-not (Test-Path $script)) { throw "Нет hardware_profile.py: $script" }
$json = & $python -X utf8 $script --json
if ($LASTEXITCODE -ne 0) { throw "hardware_profile.py завершился с кодом $LASTEXITCODE" }
$json | Set-Content -LiteralPath $out -Encoding utf8
Write-Host "Профиль записан: $out"
