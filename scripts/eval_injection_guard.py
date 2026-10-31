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
    compute_metrics,
    evaluate,
    load_samples,
)


def _slug(model: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in model).strip("-")


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


def run_ollama(
    samples,  # noqa: ANN001
    model: str,
    min_precision: float,
    max_tokens: int,
) -> int:
    """Прогон локальной моделью Ollama: ответ INJECTION/BENIGN."""
    import asyncio
    import time

    import httpx

    from app.ingestion.llm_guard import (
        INJECTION_LABEL,
        UNKNOWN_LABEL,
        classify_text,
    )

    base = "http://127.0.0.1:11434"
    try:
        tags = httpx.get(f"{base}/api/tags", timeout=10).json()
    except httpx.HTTPError as exc:
        print(f"Ollama недоступна: {exc}")
        return 2
    names = [str(item.get("name", "")) for item in tags.get("models", [])]
    if model not in names and not any(
        name.split(":")[0] == model.split(":")[0] for name in names
    ):
        print(f"модели {model} нет в реестре: {', '.join(names)}")
        return 2

    async def run_all() -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        started = time.perf_counter()
        for index, sample in enumerate(samples, start=1):
            answer = await classify_text(
                sample.text, model=model, base_url=base, max_tokens=max_tokens
            )
            rows.append(
                {
                    "text": sample.text,
                    "label": sample.label,
                    "model_label": answer.label,
                    "raw": answer.raw,
                    "latency_ms": answer.latency_ms,
                }
            )
            elapsed = time.perf_counter() - started
            print(
                f"\r{index}/{len(samples)}  прошло {elapsed:5.0f} с",
                end="",
                flush=True,
            )
        print()
        return rows

    rows = asyncio.run(run_all())
    scores = [1.0 if row["model_label"] == INJECTION_LABEL else 0.0 for row in rows]
    metrics = compute_metrics(samples, scores, 0.5)
    unknown = sum(1 for row in rows if row["model_label"] == UNKNOWN_LABEL)
    avg_ms = sum(int(row["latency_ms"]) for row in rows) // len(rows)  # type: ignore[arg-type]

    false_positive = [
        row["text"] for row in rows if row["label"] == "benign" and row["model_label"] == INJECTION_LABEL
    ]
    false_negative = [
        row["text"] for row in rows if row["label"] == "injection" and row["model_label"] != INJECTION_LABEL
    ]

    print(f"\nмодель: {model}")
    print(
        f"precision {metrics['precision']:.3f}  recall {metrics['recall']:.3f}  "
        f"f1 {metrics['f1']:.3f}  tp {metrics['tp']}  fp {metrics['fp']} "
        f"fn {metrics['fn']}  неясных ответов {unknown}"
    )
    print(f"среднее время ответа: {avg_ms} мс")
    if false_positive:
        print("\nложные срабатывания на обычном тексте:")
        for text in false_positive[:5]:
            print(f"  - {text[:110]}")
    if false_negative:
        print("\nпропущенные попытки:")
        for text in false_negative[:5]:
            print(f"  - {text[:110]}")

    out = ROOT / "docs" / f"guard-eval-{_slug(model)}.json"
    out.write_text(
        json.dumps(
            {"model": model, "metrics": metrics, "unknown": unknown, "rows": rows},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nподробности: {out}")

    if float(metrics["precision"]) < min_precision:
        print(
            "НЕ ПРОШЛО: на обычных документах слишком много ложных срабатываний — "
            "такой фильтр включать рано"
        )
        return 1
    print("ПРОШЛО по точности: можно рассматривать как временный фильтр")
    return 0


def run_model(samples, model: str, min_precision: float) -> int:  # noqa: ANN001
    """Прогон настоящим классификатором (ONNX или transformers)."""
    import asyncio
    import time

    from app.ingestion.injection_guard import availability, score_text

    ok, reason = availability(model)
    if not ok:
        print(f"фильтр недоступен: {reason}")
        return 2

    async def score_all() -> list[float]:
        scores: list[float] = []
        started = time.perf_counter()
        for index, sample in enumerate(samples, start=1):
            scores.append(await score_text(sample.text, model=model))
            print(
                f"\r{index}/{len(samples)}  прошло {time.perf_counter() - started:5.1f} с",
                end="",
                flush=True,
            )
        print()
        return scores

    scores = asyncio.run(score_all())
    rows = evaluate(samples, scores)
    print(
        f"\n{'порог':>6} {'precision':>10} {'recall':>7} {'f1':>6} "
        f"{'tp':>4} {'fp':>4} {'fn':>4}"
    )
    for row in rows:
        print(
            f"{row['threshold']:>6.1f} {row['precision']:>10.3f} "
            f"{row['recall']:>7.3f} {row['f1']:>6.3f} "
            f"{row['tp']:>4} {row['fp']:>4} {row['fn']:>4}"
        )

    best = choose_threshold(samples, scores, min_precision=min_precision)
    print(
        f"\nрекомендация: INJECTION_GUARD_THRESHOLD={best['threshold']:.1f} "
        f"(precision {best['precision']:.2f}, recall {best['recall']:.2f})"
    )
    print(f"примечание: {best['note']}")

    out = ROOT / "docs" / f"guard-eval-{_slug(Path(model).name)}.json"
    out.write_text(
        json.dumps(
            {
                "model": model,
                "metrics": rows,
                "recommended": best,
                "rows": [
                    {"text": sample.text, "label": sample.label, "score": round(score, 4)}
                    for sample, score in zip(samples, scores, strict=True)
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"подробности: {out}")

    if float(best["precision"]) < min_precision:
        print("НЕ ПРОШЛО: точность ниже требуемой — включать рано")
        return 1
    print("ПРОШЛО: порог с приемлемой точностью найден")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--predictions", default=None)
    parser.add_argument(
        "--model",
        default=None,
        help="локальный каталог модели (ONNX или transformers) для проверки",
    )
    parser.add_argument(
        "--ollama",
        default=None,
        help="прогнать локальной моделью Ollama (например qvikhr:latest)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=64,
        help="предел ответа модели: qvikhr рассуждает перед меткой",
    )
    parser.add_argument("--min-precision", type=float, default=MIN_PRECISION)
    args = parser.parse_args()

    samples = load_samples(Path(args.dataset))
    benign = sum(1 for sample in samples if sample.label == "benign")
    print(f"набор: {len(samples)} фрагментов (обычных {benign}, попыток {len(samples) - benign})")

    if args.ollama:
        return run_ollama(samples, args.ollama, args.min_precision, args.max_tokens)
    if args.model:
        return run_model(samples, args.model, args.min_precision)

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
