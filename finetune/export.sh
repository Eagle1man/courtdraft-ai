#!/usr/bin/env bash
set -e

echo "=== Слияние LoRA-адаптера с базовой моделью (ms-swift export) ==="
echo

if ! command -v swift >/dev/null 2>&1; then
  echo "[ERROR] swift не найден. Установи ms-swift: pip install \"ms-swift[all]\""
  exit 1
fi

# Путь к адаптерам (ckpt). Может быть output/vikhr-7b-instruct_0.2/.../checkpoint-XXX
if [ -z "$ADAPTERS" ]; then
  echo "[WARN] ADAPTERS не задан. Ищу последний checkpoint в ./output"
  # try to find
  LAST=$(find ./output -name "adapter_model.bin" -o -name "adapter_model.safetensors" 2>/dev/null | head -n1)
  if [ -z "$LAST" ]; then
    echo "[ERROR] Не найден адаптер. Задай ADAPTERS=/путь/к/папке с adapter_model.*"
    echo "        Пример: export ADAPTERS=./output/AI-ModelScope/Vikhr-7B-instruct_0.2/vX-XXXXXX/checkpoint-YYYY"
    exit 1
  fi
  ADAPTERS=$(dirname "$LAST")
fi

OUT_MERGED="${OUT_MERGED:-./vikhr-7b-qd-merged}"

echo "[INFO] Адаптеры: $ADAPTERS"
echo "[INFO] Выход:    $OUT_MERGED"
echo

swift export \
  --adapters "$ADAPTERS" \
  --merge-lora true \
  --save-dir "$OUT_MERGED"

echo
echo "[DONE] Слитая модель сохранена в: $OUT_MERGED"
echo "       Дальше — конвертация в GGUF (см. README.md)"
