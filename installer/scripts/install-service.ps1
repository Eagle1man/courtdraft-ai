param(
    [Parameter(Mandatory = $true)][string]$InstallDir
)
$ErrorActionPreference = "Stop"
$serviceExe = Join-Path $InstallDir "vendor\courtdraft-service.exe"
$serviceXml = Join-Path $InstallDir "vendor\courtdraft-service.xml"

if (-not (Test-Path $serviceExe)) { throw "Нет WinSW: $serviceExe" }
if (-not (Test-Path $serviceXml)) { throw "Нет XML службы: $serviceXml" }

# Остановить и удалить старую службу, если есть.
& $serviceExe stop 2>$null
& $serviceExe uninstall 2>$null

& $serviceExe install
if ($LASTEXITCODE -ne 0) { throw "WinSW install завершился с кодом $LASTEXITCODE" }
& $serviceExe start
if ($LASTEXITCODE -ne 0) { throw "WinSW start завершился с кодом $LASTEXITCODE" }

$svc = Get-Service -Name "CourtDraftAI" -ErrorAction SilentlyContinue
if (-not $svc) { throw "Служба CourtDraftAI не появилась" }
Write-Host "Служба установлена: $($svc.Status)"
