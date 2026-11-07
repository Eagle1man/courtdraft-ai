"""QLoRA для Qwen2.5-7B на Kaggle: transformers + peft + bitsandbytes.

Отличия от 1.5B-версии — только ресурсы, логика та же:

  * 4-битная загрузка и gradient checkpointing: 7B в fp16 не влезает в T4;
  * батч 1 и накопление 8: эффективный батч тот же, память меньше;
  * длина 2048 вместо 3072: 7B на 3072 упирается в память даже с чекпоинтингом;
  * учим только на ответе: у токенов промпта labels = -100;
  * при обрезке сохраняется конец ответа, а не начало промпта.

Время проверено на живом прогоне 07.11.2026: 84,7 с на шаг, то есть
3 эпохи (516 шагов) — это 12 ч 45 мин и обрыв по лимиту Kaggle (12 ч)
на 93 %, без сохранённого адаптера. Поэтому 2 эпохи (344 шага ≈ 8,5 ч)
и сохранение чекпоинтов каждые 50 шагов: обрыв больше не оставляет
пустой вывод. Ошибки пишутся в `error.txt` в выводе ядра: лог через
CLI приходит не всегда.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import traceback

subprocess.run(
    [sys.executable, "-m", "pip", "install", "-q", "-U", "bitsandbytes>=0.46.1"],
    check=True,
)

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

MODEL = "Qwen/Qwen2.5-7B-Instruct"
MAX_SEQ = 2048
EPOCHS = 2.0
LR = 1e-4
BATCH = 1
GRAD_ACCUM = 8
OUTPUT = "/kaggle/working/adapter"
FILE_NAME = "sft-established-mixed.jsonl"


def find_data() -> pathlib.Path:
    print("содержимое /kaggle/input:", flush=True)
    for root in sorted(pathlib.Path("/kaggle/input").iterdir()):
        print(" ", root, flush=True)
        if root.is_dir():
            for child in sorted(root.iterdir()):
                print("    ", child.name, flush=True)
    for candidate in pathlib.Path("/kaggle/input").rglob(FILE_NAME):
        print("данные найдены:", candidate, flush=True)
        return candidate
    raise SystemExit(f"нет файла {FILE_NAME}")


def load_items(path: pathlib.Path) -> tuple[list[dict], list[dict]]:
    items = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    train = [item for item in items if (item.get("meta") or {}).get("split") != "val"]
    val = [item for item in items if (item.get("meta") or {}).get("split") == "val"]
    if len(val) < 20:
        cut = max(1, len(items) // 20)
        train, val = items[:-cut], items[-cut:]
    print(f"примеров: {len(items)}, train {len(train)}, val {len(val)}", flush=True)
    return train, val


def main() -> None:
    data_path = find_data()
    train_items, val_items = load_items(data_path)

    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def encode(item: dict) -> dict:
        messages = item["messages"]
        prompt_text = tokenizer.apply_chat_template(
            messages[:-1], tokenize=False, add_generation_prompt=True
        )
        answer = messages[-1]["content"]
        prompt_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
        answer_ids = tokenizer(answer, add_special_tokens=False)["input_ids"]
        answer_ids = answer_ids + [tokenizer.eos_token_id]
        input_ids = prompt_ids + answer_ids
        labels = [-100] * len(prompt_ids) + answer_ids
        if len(input_ids) > MAX_SEQ:
            input_ids = input_ids[-MAX_SEQ:]
            labels = labels[-MAX_SEQ:]
        return {
            "input_ids": input_ids,
            "labels": labels,
            "attention_mask": [1] * len(input_ids),
        }

    train_set = Dataset.from_list([encode(item) for item in train_items])
    val_set = Dataset.from_list([encode(item) for item in val_items])

    def collate(batch: list[dict]) -> dict:
        longest = max(len(row["input_ids"]) for row in batch)
        pad_id = tokenizer.pad_token_id
        input_ids, labels, masks = [], [], []
        for row in batch:
            padding = longest - len(row["input_ids"])
            input_ids.append(row["input_ids"] + [pad_id] * padding)
            labels.append(row["labels"] + [-100] * padding)
            masks.append(row["attention_mask"] + [0] * padding)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
            "attention_mask": torch.tensor(masks, dtype=torch.long),
        }

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=bnb, device_map="auto", trust_remote_code=True
    )
    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=True
    )
    model = get_peft_model(
        model,
        LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
        ),
    )
    model.config.use_cache = False
    model.print_trainable_parameters()

    import inspect

    candidate = {
        "output_dir": OUTPUT,
        "per_device_train_batch_size": BATCH,
        "per_device_eval_batch_size": BATCH,
        "gradient_accumulation_steps": GRAD_ACCUM,
        "num_train_epochs": EPOCHS,
        "learning_rate": LR,
        "lr_scheduler_type": "cosine",
        "warmup_ratio": 0.03,
        "warmup_steps": 10,
        "logging_steps": 10,
        "eval_strategy": "steps",
        "evaluation_strategy": "steps",
        "eval_steps": 100,
        "save_strategy": "steps",
        "save_steps": 50,
        "save_total_limit": 1,
        "fp16": True,
        "optim": "adamw_8bit",
        "weight_decay": 0.01,
        "report_to": [],
        "remove_unused_columns": False,
        "gradient_checkpointing": True,
        "seed": 3407,
    }
    allowed = set(inspect.signature(TrainingArguments.__init__).parameters)
    kwargs = {key: value for key, value in candidate.items() if key in allowed}
    print("аргументы обучения:", sorted(kwargs), flush=True)

    trainer = Trainer(
        model=model,
        args=TrainingArguments(**kwargs),
        train_dataset=train_set,
        eval_dataset=val_set,
        data_collator=collate,
    )
    result = trainer.train()
    print("train_loss:", result.training_loss, flush=True)

    model.save_pretrained(OUTPUT)
    tokenizer.save_pretrained(OUTPUT)
    print("адаптер сохранён:", OUTPUT, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        text = traceback.format_exc()
        pathlib.Path("/kaggle/working/error.txt").write_text(text, encoding="utf-8")
        print(text, file=sys.stderr, flush=True)
        raise
