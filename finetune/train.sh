#!/usr/bin/env bash
set -e

echo "=== Vikhr 7B QLoRA finetune via ms-swift ==="
echo "Дата: $(date)"
echo

# Проверка версии ms-swift
if command -v swift >/dev/null 2>&1; then
  echo "[INFO] swift найден: $(swift --version 2>&1 | head -n1)"
else
  echo "[WARN] swift не найден в PATH. Установи: pip install \"ms-swift[all]\" modelscope"
  echo "[WARN] Пробуем продолжить..."
fi

DATASET="${DATASET:-./dataset.jsonl}"
MODEL="${MODEL:-AI-ModelScope/Vikhr-7B-instruct_0.2}"
OUTPUT_DIR="${OUTPUT_DIR:-./output}"

if [ ! -f "$DATASET" ]; then
  echo "[ERROR] Не найден датасет: $DATASET"
  echo "        Скопируй dataset.jsonl рядом с finetune/ или задай DATASET=/путь/к/dataset.jsonl"
  exit 1
fi

echo "[INFO] Модель: $MODEL"
echo "[INFO] Датасет: $DATASET"
echo "[INFO] Выход:   $OUTPUT_DIR"
echo

# Совместимость версий ms-swift 2.x (sft_type) и 3.x (train_type)
# Попробуем 3.x стиль, если не сработает — можно поправить флаги вручную
swift sft \
  --model "$MODEL" \
  --dataset "$DATASET" \
  --train_type lora \
  --quantization_bit 4 \
  --target_modules all-linear \
  --lora_rank 8 \
  --lora_alpha 32 \
  --learning_rate 1e-4 \
  --num_train_epochs 2 \
  --per_device_train_batch_size 2 \
  --gradient_accumulation_steps 8 \
  --max_length 2048 \
  --split_dataset_ratio 0 \
  --save_total_limit 2 \
  --output_dir "$OUTPUT_DIR"
