param(
    [Parameter(Mandatory = $true)][string]$InstallDir
)
$serviceExe = Join-Path $InstallDir "vendor\courtdraft-service.exe"
if (Test-Path $serviceExe) {
    & $serviceExe stop 2>$null
    & $serviceExe uninstall 2>$null
}
