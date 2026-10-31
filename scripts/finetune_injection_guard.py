"""Дообучение фильтра инструкций на русском наборе.

Зачем: готовая Prompt Guard 2 на русских перефразах даёт recall 0,17 —
она не обобщает. Своя маленькая модель на русских данных решает задачу
точнее и занимает сотни мегабайт.

Обучение — на ВМ (GPU не обязателен, но на CPU это часы). Здесь — сам
скрипт и проверка данных; запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\build_injection_dataset.py
    backend\\.venv\\Scripts\\python.exe scripts\\finetune_injection_guard.py --dry-run
    backend\\.venv\\Scripts\\python.exe scripts\\finetune_injection_guard.py

Базы на выбор:

- `microsoft/mdeberta-v3-base` (та же архитектура, что у Prompt Guard,
  мультиязычная, ~1,1 ГБ) — рекомендуемая;
- `DeepPavlov/rubert-base-cased` (русская, ~700 МБ) — если нужен
  меньший размер;
- `cointegrated/rubert-tiny2` (~120 МБ) — быстрый черновик.

Критерий приёмки: на контрольном наборе `backend/tests/data/injection_guard_set.jsonl`
precision не ниже 0,90, recall не ниже 0,70. Иначе модель не включается.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "injection_guard"
HOLDOUT = ROOT / "backend" / "tests" / "data" / "injection_guard_set.jsonl"
DEFAULT_BASE = "microsoft/mdeberta-v3-base"


def load_jsonl(path: Path) -> list[dict[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def dry_run(data_dir: Path, base: str) -> int:
    print(f"база: {base}")
    for name in ("train", "val", "test"):
        path = data_dir / f"{name}.jsonl"
        if not path.exists():
            print(f"нет файла: {path} — сначала build_injection_dataset.py")
            return 2
        rows = load_jsonl(path)
        labels = {row["label"] for row in rows}
        print(f"{name}: {len(rows)} строк, метки {sorted(labels)}")
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as exc:
        print(f"нет зависимостей: {exc}")
        return 2
    print("holdout для приёмки:", HOLDOUT.exists())
    print("план: 3 эпохи, lr 2e-5, max_len 256; запуск без --dry-run")
    return 0


def train(args: argparse.Namespace) -> int:
    import numpy as np
    import torch
    from torch.utils.data import Dataset
    from transformers import (
        AutoModelForSequenceClassification,
        AutoTokenizer,
        Trainer,
        TrainingArguments,
    )

    labels = {"benign": 0, "injection": 1}

    class Samples(Dataset):
        def __init__(self, rows: list[dict[str, str]]) -> None:
            self.rows = rows

        def __len__(self) -> int:
            return len(self.rows)

        def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
            row = self.rows[index]
            encoded = tokenizer(
                row["text"],
                truncation=True,
                max_length=args.max_length,
                padding="max_length",
            )
            item = {key: torch.tensor(value) for key, value in encoded.items()}
            item["labels"] = torch.tensor(labels[row["label"]])
            return item

    rows_train = load_jsonl(Path(args.data) / "train.jsonl")
    rows_val = load_jsonl(Path(args.data) / "val.jsonl")

    tokenizer = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base,
        num_labels=2,
        id2label={0: "BENIGN", 1: "MALICIOUS"},
        label2id={"BENIGN": 0, "MALICIOUS": 1},
    )

    def metrics(eval_pred: tuple[np.ndarray, np.ndarray]) -> dict[str, float]:
        logits, true = eval_pred
        predictions = np.argmax(logits, axis=-1)
        tp = int(((predictions == 1) & (true == 1)).sum())
        fp = int(((predictions == 1) & (true == 0)).sum())
        fn = int(((predictions == 0) & (true == 1)).sum())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {"precision": precision, "recall": recall, "f1": f1}

    training_args = TrainingArguments(
        output_dir=args.out,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch,
        per_device_eval_batch_size=args.batch,
        learning_rate=args.lr,
        eval_strategy="epoch",
        save_strategy="epoch",
        logging_steps=50,
        seed=20261101,
        report_to=[],
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=Samples(rows_train),
        eval_dataset=Samples(rows_val),
        compute_metrics=metrics,
    )
    trainer.train()
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    print(f"модель сохранена: {args.out}")

    if HOLDOUT.exists():
        print(
            "Проверка на контрольном наборе:\n"
            f"  backend\\.venv\\Scripts\\python.exe scripts\\eval_injection_guard.py "
            f"--model {args.out}"
        )
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--out", default=str(ROOT / "models" / "injection-guard-ru"))
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument(
        "--dry-run", action="store_true", help="проверить данные, не обучая"
    )
    args = parser.parse_args()

    if args.dry_run:
        return dry_run(Path(args.data), args.base)
    return train(args)


if __name__ == "__main__":
    sys.exit(main())
