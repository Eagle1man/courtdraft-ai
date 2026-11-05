#!/usr/bin/env bash
# Поставить дообученную модель на стенд: GGUF → Ollama → профиль 1.5B.
#
# Запуск на стенде (файл GGUF уже скопирован):
#     bash scripts/vm_install_tuned_model.sh /home/ubuntuii/model-q4_k_m.gguf
#     bash scripts/vm_install_tuned_model.sh /home/ubuntuii/model-q4_k_m.gguf courtdraft-1p5b
#
# Что делает по порядку: проверяет файл, создаёт Modelfile рядом с ним,
# собирает модель в Ollama, проверяет коротким ответом, переключает .env
# на неё с параметрами для 1.5B и перезапускает службу.
#
# Откат: bash scripts/vm_profile_15b.sh (вернёт qwen2.5:1.5b).
set -euo pipefail

GGUF="${1:?укажите путь к .gguf}"
NAME="${2:-courtdraft-1p5b}"

if [ ! -f "$GGUF" ]; then
    echo "нет файла $GGUF" >&2
    exit 1
fi

SIZE_MB=$(( $(stat -c %s "$GGUF") / 1024 / 1024 ))
echo "файл: $GGUF (${SIZE_MB} МБ)"
if [ "$SIZE_MB" -lt 300 ]; then
    echo "файл подозрительно мал для квантованной модели — проверьте, что скачался GGUF, а не адаптер" >&2
    exit 1
fi

MODELFILE="$(dirname "$GGUF")/Modelfile.${NAME}"
cat > "$MODELFILE" <<EOF
FROM ${GGUF}
PARAMETER temperature 0.1
PARAMETER num_ctx 4096
PARAMETER stop "<|im_end|>"
EOF
echo "Modelfile: $MODELFILE"

ollama create "$NAME" -f "$MODELFILE"
ollama list | grep "$NAME"

echo "--- проверка коротким ответом ---"
curl -s -m 300 http://127.0.0.1:11434/api/generate \
    -d "{\"model\":\"${NAME}\",\"prompt\":\"Перечисли виды судопроизводства. Кратко.\",\"stream\":false,\"options\":{\"num_predict\":32}}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('response','НЕТ ОТВЕТА')[:200])"

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
set_env OLLAMA_MODEL "$NAME"
set_env DRAFT_MAX_TOKENS "420"
set_env DRAFT_MAX_TOKENS_RETRY "760"
set_env DRAFT_CONTEXT_CHARS "4000"
grep -nE '^(OLLAMA_MODEL|DRAFT_MAX_TOKENS|DRAFT_CONTEXT_CHARS)=' .env

pid=$(systemctl show courtdraft-backend -p MainPID --value)
echo "перезапуск pid=$pid"
kill -9 "$pid"
sleep 15
systemctl is-active courtdraft-backend
curl -s -m 15 http://127.0.0.1:8081/api/health | head -c 250
echo
echo "готово. Откат: bash scripts/vm_profile_15b.sh"
