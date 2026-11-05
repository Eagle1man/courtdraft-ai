#!/usr/bin/env bash
# Ночной профиль 7B: параметры из docs/7b-tuning.md.
#
# Модель qwen2.5:7b, а не wavecut/vikhr:7b: Vikhr собран как completion-
# модель без chat-шаблона, а сервис зовёт /api/chat. Смена семейства
# моделей ещё и смешала бы два фактора — размер и саму модель, — и вывод
# «7B лучше» был бы неверным.
#
# Запуск на стенде:
#     ollama pull qwen2.5:7b        # один раз, 4,7 ГБ
#     bash scripts/vm_profile_7b.sh
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

set_env OLLAMA_MODEL "qwen2.5:7b"
set_env DRAFT_MAX_TOKENS "1200"
set_env DRAFT_MAX_TOKENS_RETRY "1800"
set_env DRAFT_CONTEXT_CHARS "6000"
set_env OLLAMA_NUM_CTX "8192"
set_env OLLAMA_TIMEOUT_SEC "900"

grep -nE '^(OLLAMA_MODEL|DRAFT_MAX_TOKENS|DRAFT_MAX_TOKENS_RETRY|DRAFT_CONTEXT_CHARS|OLLAMA_NUM_CTX|OLLAMA_TIMEOUT_SEC)=' .env

echo "--- модель на месте? ---"
curl -s http://127.0.0.1:11434/api/tags | python3 -c "
import json, sys
names = [m['name'] for m in json.load(sys.stdin)['models']]
found = any(n.startswith('qwen2.5:7b') for n in names)
print('qwen2.5:7b найден' if found else 'НЕТ МОДЕЛИ — сначала ollama pull qwen2.5:7b')
print('модели:', names)
"

pid=$(systemctl show courtdraft-backend -p MainPID --value)
echo "перезапуск pid=$pid"
kill -9 "$pid"
sleep 20
systemctl is-active courtdraft-backend
curl -s -m 20 http://127.0.0.1:8081/api/health | head -c 300
echo
