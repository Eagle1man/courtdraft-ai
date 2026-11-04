"""QLoRA-дообучение 7B на датасете «материалы → установительная часть».

Запускать в Kaggle Notebook с GPU (T4 x1 достаточно). Скрипт ожидает
датасет, загруженный как Kaggle Dataset, и файл JSONL нашего формата:

    {"messages": [{system}, {user}, {assistant}], "meta": {...}}

Пример запуска в ячейке Kaggle:

    !pip install -q unsloth
    !python train_qlora.py \
        --train /kaggle/input/courtdraft-sft/sft-established-feb2026.jsonl \
        --output /kaggle/working/adapter

Что важно и почему:
  * учим только на ответе ассистента (train_on_responses_only) — иначе
    модель тратит ёмкость на запоминание промпта;
  * max_seq 4096: промпт ~500 токенов, ответ до ~2 500;
  * одна-две эпохи: 1 400 примеров, больше — переобучение;
  * обучающий и проверочный файлы делятся по `meta.split`, а не
    случайно: половины одного дела не должны попасть в обе части.

Ограничение ожиданий: 1 400 примеров учат форме и связности раздела,
а не праву. Юридическую точность даёт RAG по нормам, а не этот прогон.
"""

from __future__ import annotations

import argparse
import json
import os

import torch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", required=True, help="JSONL с примерами")
    parser.add_argument("--output", default="/kaggle/working/adapter")
    parser.add_argument(
        "--model",
        default="Qwen/Qwen2.5-7B-Instruct",
        help="Базовая модель. Vikhr-7B тоже подойдёт, но требует trust_remote_code",
    )
    parser.add_argument("--max-seq", type=int, default=4096)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="0 — все примеры")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    from datasets import load_dataset
    from transformers import TrainingArguments
    from trl import SFTTrainer
    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template, train_on_responses_only

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model,
        max_seq_length=args.max_seq,
        dtype=None,
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=16,
        lora_alpha=32,
        lora_dropout=0.0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
    )
    tokenizer = get_chat_template(tokenizer, chat_template="chatml")

    def to_text(batch: dict) -> dict:
        texts = []
        for messages in batch["messages"]:
            texts.append(
                tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=False
                )
            )
        return {"text": texts}

    dataset = load_dataset("json", data_files=args.train, split="train")
    if args.limit:
        dataset = dataset.select(range(min(args.limit, len(dataset))))
    dataset = dataset.map(to_text, batched=True, remove_columns=dataset.column_names)

    # Проверочная часть — из meta.split. Если её нет, берём 5 % хвоста:
    # обучение без контроля не даёт понять, переобучились мы или нет.
    meta = load_dataset("json", data_files=args.train, split="train")
    val_indexes = [
        index
        for index, item in enumerate(meta)
        if json.loads(json.dumps(item.get("meta") or {})).get("split") == "val"
    ]
    if args.limit:
        val_indexes = [index for index in val_indexes if index < args.limit]
    if len(val_indexes) >= 20:
        train_set = dataset.select(
            [i for i in range(len(dataset)) if i not in set(val_indexes)]
        )
        val_set = dataset.select(val_indexes)
    else:
        split = dataset.train_test_split(test_size=0.05, seed=3407)
        train_set, val_set = split["train"], split["test"]

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_set,
        eval_dataset=val_set,
        dataset_text_field="text",
        max_seq_length=args.max_seq,
        args=TrainingArguments(
            output_dir=args.output,
            per_device_train_batch_size=args.batch,
            gradient_accumulation_steps=args.grad_accum,
            num_train_epochs=args.epochs,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_ratio=0.03,
            logging_steps=10,
            eval_strategy="steps",
            eval_steps=50,
            save_strategy="no",
            fp16=not torch.cuda.is_bf16_supported(),
            bf16=torch.cuda.is_bf16_supported(),
            optim="adamw_8bit",
            weight_decay=0.01,
            seed=3407,
            report_to="none",
        ),
    )
    trainer = train_on_responses_only(
        trainer,
        instruction_part="<|im_start|>user\n",
        response_part="<|im_start|>assistant\n",
    )

    result = trainer.train()
    print("train_loss:", result.training_loss)

    model.save_pretrained(args.output)
    tokenizer.save_pretrained(args.output)
    print("adapter saved:", args.output)


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
