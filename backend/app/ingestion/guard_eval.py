"""Разметка и метрики для смыслового фильтра инструкций.

Зачем отдельный модуль: порог фильтра нельзя выбирать «на глаз». Нужен
небольшой размеченный набор и честная precision/recall — иначе
предупреждение появится на каждом втором документе и его перестанут
читать.

Набор лежит в `backend/tests/data/injection_guard_set.jsonl`: обычные
юридические фрагменты и перефразированные попытки. Фрагменты
синтетические — персональных данных в наборе нет.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASET = (
    Path(__file__).resolve().parents[2] / "tests" / "data" / "injection_guard_set.jsonl"
)
LABELS = ("benign", "injection")
#: Порог, при котором предупреждения ещё можно не закрывать не читая.
MIN_PRECISION = 0.9


@dataclass(frozen=True)
class Sample:
    text: str
    label: str


def load_samples(path: Path = DATASET) -> list[Sample]:
    """Прочитать набор. Кривая строка — ошибка, а не «пропустим»."""
    if not path.exists():
        raise FileNotFoundError(f"нет набора для оценки: {path}")
    samples: list[Sample] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"строка {number}: неверный JSON: {exc}") from exc
        text = str(row.get("text", "")).strip()
        label = str(row.get("label", "")).strip()
        if not text:
            raise ValueError(f"строка {number}: пустой текст")
        if label not in LABELS:
            raise ValueError(f"строка {number}: неизвестная метка {label!r}")
        samples.append(Sample(text=text, label=label))
    return samples


def compute_metrics(
    samples: list[Sample], scores: list[float], threshold: float
) -> dict[str, float | int]:
    """Precision/recall при пороге: score >= threshold считается тревогой."""
    if len(samples) != len(scores):
        raise ValueError("чисел и фрагментов разное количество")
    tp = fp = fn = tn = 0
    for sample, score in zip(samples, scores, strict=True):
        flagged = score >= threshold
        if flagged and sample.label == "injection":
            tp += 1
        elif flagged and sample.label == "benign":
            fp += 1
        elif not flagged and sample.label == "injection":
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "threshold": threshold,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def evaluate(
    samples: list[Sample],
    scores: list[float],
    *,
    thresholds: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9),
) -> list[dict[str, float | int]]:
    return [compute_metrics(samples, scores, threshold) for threshold in thresholds]


def choose_threshold(
    samples: list[Sample],
    scores: list[float],
    *,
    thresholds: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9),
    min_precision: float = MIN_PRECISION,
) -> dict[str, float | int | str]:
    """Порог: максимальный recall при precision >= min_precision.

    Если ни один порог не даёт нужной точности, возвращается лучший по F1
    с пометкой — это честнее, чем молча выдать порог, который шумит.
    """
    rows = evaluate(samples, scores, thresholds=thresholds)
    eligible = [row for row in rows if float(row["precision"]) >= min_precision]
    if eligible:
        best = max(eligible, key=lambda row: (float(row["recall"]), float(row["f1"])))
        return {**best, "note": "точность не ниже требуемой"}
    best = max(rows, key=lambda row: float(row["f1"]))
    return {
        **best,
        "note": (
            f"ни один порог не даёт precision >= {min_precision:.0%}: "
            "фильтр на этом наборе ненадёжен, нужен больше размеченных данных"
        ),
    }
