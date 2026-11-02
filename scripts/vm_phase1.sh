#!/bin/bash
# Первый этап теста: все дела на быстрой модели, квота сильной выключена.
set -e
cd /home/ubuntuii/courtdraft-ai

set_env() {
  key="$1"
  value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s#^${key}=.*#${key}=${value}#" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

set_env OLLAMA_MODEL qwen2.5:1.5b
set_env MODEL_ROUTING_ENABLED true
set_env STRONG_MODEL 'wavecut/vikhr:7b-instruct_0.4-Q4_1'
set_env WEAK_MODEL qwen2.5:1.5b
set_env STRONG_MODEL_QUOTA 0

grep -E '^(OLLAMA_MODEL|MODEL_ROUTING_ENABLED|STRONG_MODEL|WEAK_MODEL|STRONG_MODEL_QUOTA)=' .env

systemctl restart courtdraft-backend
sleep 12
systemctl is-active courtdraft-backend
curl -s -m 20 http://127.0.0.1:8081/api/health | head -c 500
echo
