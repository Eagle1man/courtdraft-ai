"""Оценка фильтра инструкций на размеченном наборе: precision/recall.

Зачем: порог фильтра — это компромисс между «пропустить попытку» и
«предупредить на обычном договоре». Выбирается по цифрам, а не на глаз.

Два режима:

- **с моделью** (после установки Prompt Guard): считает оценки сам;
- **с готовыми оценками** (`--predictions`): JSONL вида
  `{"text": "...", "score": 0.93}` — можно прогнать на другой машине
  или пересчитать пороги без модели.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\eval_injection_guard.py
    backend\\.venv\\Scripts\\python.exe scripts\\eval_injection_guard.py --predictions scores.jsonl

Коды: 0 — порог с нужной точностью найден; 1 — набор слишком мал или
модель шумит; 2 — модель недоступна и оценок нет.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ingestion.guard_eval import (  # noqa: E402
    DATASET,
    MIN_PRECISION,
    choose_threshold,
    evaluate,
    load_samples,
)


def read_predictions(path: Path) -> dict[str, float]:
    scores: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        scores[str(row["text"]).strip()] = float(row["score"])
    return scores


async def score_with_model(texts: list[str]) -> list[float]:
    from app.ingestion.injection_guard import availability, score_text

    ok, reason = availability()
    if not ok:
        raise RuntimeError(reason)
    scores: list[float] = []
    for index, text in enumerate(texts, start=1):
        print(f"\rоценка {index}/{len(texts)}", end="", flush=True)
        scores.append(await score_text(text))
    print()
    return scores


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--predictions", default=None)
    parser.add_argument("--min-precision", type=float, default=MIN_PRECISION)
    args = parser.parse_args()

    samples = load_samples(Path(args.dataset))
    benign = sum(1 for sample in samples if sample.label == "benign")
    print(f"набор: {len(samples)} фрагментов (обычных {benign}, попыток {len(samples) - benign})")

    if args.predictions:
        table = read_predictions(Path(args.predictions))
        missing = [sample.text for sample in samples if sample.text not in table]
        if missing:
            print(f"в файле оценок нет {len(missing)} фрагментов — начинаю с первого")
            return 2
        scores = [table[sample.text] for sample in samples]
    else:
        try:
            scores = asyncio.run(score_with_model([sample.text for sample in samples]))
        except RuntimeError as exc:
            print(f"модель недоступна: {exc}")
            print("Оценки можно посчитать на другой машине и подать через --predictions.")
            return 2

    rows = evaluate(samples, scores)
    print(f"\n{'порог':>6} {'precision':>10} {'recall':>7} {'f1':>6} {'tp':>4} {'fp':>4} {'fn':>4}")
    for row in rows:
        print(
            f"{row['threshold']:>6.1f} {row['precision']:>10.3f} {row['recall']:>7.3f} "
            f"{row['f1']:>6.3f} {row['tp']:>4} {row['fp']:>4} {row['fn']:>4}"
        )

    best = choose_threshold(samples, scores, min_precision=args.min_precision)
    print(
        f"\nрекомендация: INJECTION_GUARD_THRESHOLD={best['threshold']:.1f} "
        f"(precision {best['precision']:.2f}, recall {best['recall']:.2f})"
    )
    print(f"примечание: {best['note']}")

    if float(best["precision"]) < args.min_precision:
        print("НЕ ПРОШЛО: при таком наборе фильтр шумит, включать рано")
        return 1
    print("ПРОШЛО: порог с приемлемой точностью найден")
    return 0


if __name__ == "__main__":
    sys.exit(main())
