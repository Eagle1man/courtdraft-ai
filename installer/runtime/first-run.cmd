@echo off
setlocal
set "ROOT=%~dp0.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\first-run.ps1" -InstallDir "%ROOT%"
pause
