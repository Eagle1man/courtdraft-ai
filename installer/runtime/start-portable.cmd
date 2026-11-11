@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\backend"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%ROOT%\.env" (
  echo Нет .env. Запустите "Настройка моделей".
  pause
  exit /b 2
)
start "" http://127.0.0.1:8080/app
"%ROOT%\runtime\python\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8080
pause
