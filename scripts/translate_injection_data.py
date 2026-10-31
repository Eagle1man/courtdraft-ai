"""Перевод открытого набора инъекций на русский — локально, без облака.

Источник: `xTRam1/safe-guard-prompt-injection` (MIT, англ., 8 236 + 2 060
примеров). Переводит `Helsinki-NLP/opus-mt-en-ru` (CC-BY-4.0, ~300 МБ).

Зачем: синтетических шаблонов мало, живые попытки формулируются иначе.
Перевод — не замена ручной проверке: машинный перевод ломает смысл, и
10–20 строк каждого набора надо прочитать глазами.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\translate_injection_data.py `
        --source artifacts\\xtram-test.parquet --limit-per-class 150

Пишет `data\\translated_injections.jsonl`: {"text", "label", "source",
"original"}.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "translated_injections.jsonl"
MODEL = "Helsinki-NLP/opus-mt-en-ru"
SEED = 20261101


def read_parquet(path: Path) -> tuple[list[str], list[int]]:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    return list(table.column("text").to_pylist()), list(table.column("label").to_pylist())


def pick(texts: list[str], labels: list[int], limit_per_class: int) -> list[tuple[str, str]]:
    """Детерминированная выборка: по limit на каждый класс."""
    rnd = random.Random(SEED)
    picked: list[tuple[str, str]] = []
    for value, label in ((1, "injection"), (0, "benign")):
        pool = [text for text, mark in zip(texts, labels, strict=True) if mark == value]
        rnd.shuffle(pool)
        for text in pool[:limit_per_class]:
            cleaned = " ".join(str(text).split())
            if 20 <= len(cleaned) <= 1200 and not any("а" <= ch.lower() <= "я" for ch in cleaned):
                picked.append((cleaned, label))
    return picked


def translate(pairs: list[tuple[str, str]], model_name: str, batch: int) -> list[dict[str, str]]:
    import torch
    from transformers import MarianMTModel, MarianTokenizer

    torch.set_num_threads(2)
    tokenizer = MarianTokenizer.from_pretrained(model_name)
    model = MarianMTModel.from_pretrained(model_name)
    model.eval()

    rows: list[dict[str, str]] = []
    started = time.perf_counter()
    for start in range(0, len(pairs), batch):
        chunk = pairs[start : start + batch]
        texts = [text for text, _label in chunk]
        encoded = tokenizer(
            texts, return_tensors="pt", padding=True, truncation=True, max_length=512
        )
        with torch.no_grad():
            generated = model.generate(
                **encoded, max_new_tokens=160, num_beams=2, early_stopping=True
            )
        translated = tokenizer.batch_decode(generated, skip_special_tokens=True)
        for (original, label), text in zip(chunk, translated, strict=True):
            rows.append(
                {
                    "text": text.strip(),
                    "label": label,
                    "source": "xtram1_translated",
                    "original": original,
                }
            )
        elapsed = time.perf_counter() - started
        print(
            f"\r{min(start + batch, len(pairs))}/{len(pairs)}  {elapsed:5.0f} с",
            end="",
            flush=True,
        )
    print()
    return rows


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="parquet из xTRam1")
    parser.add_argument("--limit-per-class", type=int, default=150)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    texts, labels = read_parquet(Path(args.source))
    pairs = pick(texts, labels, args.limit_per_class)
    print(f"к переводу: {len(pairs)} (injection {sum(1 for _, l in pairs if l == 'injection')})")
    if not pairs:
        print("нечего переводить")
        return 2

    rows = translate(pairs, args.model, args.batch)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    print(f"записано: {out} ({len(rows)} строк)")
    print("проверьте 10–20 строк глазами: машинный перевод искажает смысл")
    return 0


if __name__ == "__main__":
    sys.exit(main())
