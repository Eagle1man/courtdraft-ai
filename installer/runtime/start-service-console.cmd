@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\backend"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%ROOT%\runtime\python\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8081
pause
