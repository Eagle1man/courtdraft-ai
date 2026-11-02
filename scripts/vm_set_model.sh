#!/bin/bash
# Переключить модель в .env и поднять backend на 8081.
set -e
MODEL="$1"
cd /home/ubuntuii/courtdraft-ai
sed -i "s#^OLLAMA_MODEL=.*#OLLAMA_MODEL=$MODEL#" .env
grep -E '^(OLLAMA_MODEL|LLM_PROVIDER|EMBEDDING_PROVIDER|DATABASE_URL)=' .env

cd backend
setsid nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8081 \
  > /home/ubuntuii/backend.log 2>&1 < /dev/null &
sleep 14
tail -4 /home/ubuntuii/backend.log
echo '--- health ---'
curl -s -m 30 http://127.0.0.1:8081/api/health | head -c 400
echo
