#!/bin/bash
# Прописать системный каталог языков Tesseract и перезапустить backend.
set -e
cd /home/ubuntuii/courtdraft-ai
sed -i 's#^OCR_TESSDATA_DIR=.*#OCR_TESSDATA_DIR=/usr/share/tesseract-ocr/5/tessdata#' .env
grep -E '^OCR_(ENABLED|LANGUAGES|TESSDATA_DIR)=' .env

PID=$(ss -ltnp 2>/dev/null | grep ':8081' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)
echo "pid on 8081: ${PID:-none}"
if [ -n "${PID:-}" ]; then
  kill "$PID"
  sleep 4
fi

cd backend
setsid nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081 \
  > /home/ubuntuii/backend.log 2>&1 < /dev/null &
sleep 14
tail -3 /home/ubuntuii/backend.log
curl -s -m 30 http://127.0.0.1:8081/api/health | head -c 180
echo
