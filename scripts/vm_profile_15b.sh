#!/usr/bin/env bash
# Профиль 1.5B: параметры из docs/7b-tuning.md для маленькой модели.
# Используется и как откат после ночного прогона 7B.
#
# Запуск на стенде:
#     bash scripts/vm_profile_15b.sh
set -euo pipefail

cd "$HOME/courtdraft-ai"

set_env() {
    key="$1"
    value="$2"
    if grep -q "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        echo "${key}=${value}" >> .env
    fi
}

set_env OLLAMA_MODEL "qwen2.5:1.5b"
set_env DRAFT_MAX_TOKENS "420"
set_env DRAFT_MAX_TOKENS_RETRY "760"
set_env DRAFT_CONTEXT_CHARS "4000"
set_env OLLAMA_NUM_CTX "4096"
set_env OLLAMA_TIMEOUT_SEC "900"

grep -nE '^(OLLAMA_MODEL|DRAFT_MAX_TOKENS|DRAFT_MAX_TOKENS_RETRY|DRAFT_CONTEXT_CHARS|OLLAMA_NUM_CTX|OLLAMA_TIMEOUT_SEC)=' .env

pid=$(systemctl show courtdraft-backend -p MainPID --value)
echo "перезапуск pid=$pid"
kill -9 "$pid"
sleep 15
systemctl is-active courtdraft-backend
curl -s -m 15 http://127.0.0.1:8081/api/health | head -c 300
echo
