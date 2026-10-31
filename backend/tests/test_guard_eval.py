"""Размеченный набор и метрики фильтра инструкций.

Набор проверяется как данные: метки допустимые, тексты не повторяются,
обычные фрагменты не должны ловиться детектором формулировок — иначе
фильтр оценивался бы на подсказках, которые и так находит правило.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingestion.guard_eval import (
    DATASET,
    choose_threshold,
    compute_metrics,
    evaluate,
    load_samples,
)
from app.ingestion.injection import find_injection


class TestDataset:
    def test_has_enough_samples(self) -> None:
        samples = load_samples()
        benign = [s for s in samples if s.label == "benign"]
        injections = [s for s in samples if s.label == "injection"]
        assert len(benign) >= 36, f"обычных фрагментов {len(benign)}"
        assert len(injections) >= 18, f"попыток {len(injections)}"

    def test_texts_are_unique(self) -> None:
        samples = load_samples()
        assert len({s.text for s in samples}) == len(samples)

    def test_benign_texts_do_not_trigger_rules(self) -> None:
        """Обычные фрагменты не должны ловиться детектором формулировок."""
        for sample in load_samples():
            if sample.label != "benign":
                continue
            assert find_injection(sample.text) == [], sample.text

    def test_rules_catch_a_part_of_injections(self) -> None:
        """Часть попыток ловится правилами — но не все, иначе фильтр не нужен."""
        injections = [s for s in load_samples() if s.label == "injection"]
        caught = sum(1 for sample in injections if find_injection(sample.text))
        assert caught >= 8, f"правилами поймано {caught} из {len(injections)}"
        assert caught < len(injections), "правила ловят всё — набор не проверяет фильтр"

    def test_bad_label_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text(json.dumps({"text": "текст", "label": "unknown"}) + "\n")
        with pytest.raises(ValueError, match="метка"):
            load_samples(path)

    def test_default_dataset_exists(self) -> None:
        assert DATASET.exists()


class TestMetrics:
    def _samples(self):
        return load_samples()

    def test_counts_at_threshold(self) -> None:
        samples = [
            s for s in self._samples() if s.label == "injection"
        ][:2] + [s for s in self._samples() if s.label == "benign"][:2]
        scores = [0.9, 0.2, 0.3, 0.8]
        row = compute_metrics(samples, scores, 0.5)
        assert (row["tp"], row["fn"], row["fp"], row["tn"]) == (1, 1, 1, 1)
        assert row["precision"] == pytest.approx(0.5)
        assert row["recall"] == pytest.approx(0.5)

    def test_evaluate_returns_every_threshold(self) -> None:
        samples = self._samples()
        scores = [0.5] * len(samples)
        rows = evaluate(samples, scores, thresholds=(0.4, 0.6))
        assert [row["threshold"] for row in rows] == [0.4, 0.6]

    def test_choose_threshold_prefers_precision(self) -> None:
        samples = [
            s for s in self._samples() if s.label == "injection"
        ][:2] + [s for s in self._samples() if s.label == "benign"][:2]
        # Попытки выше порога, обычные — ниже.
        scores = [0.95, 0.85, 0.2, 0.1]
        best = choose_threshold(samples, scores, min_precision=0.9)
        assert best["precision"] >= 0.9
        assert best["recall"] == pytest.approx(1.0)

    def test_no_eligible_threshold_is_reported(self) -> None:
        samples = self._samples()
        # Все оценки высокие: любая обычная фраза становится «тревогой».
        scores = [0.99] * len(samples)
        best = choose_threshold(samples, scores, min_precision=0.9)
        assert best["precision"] < 0.9
        assert "ненадёжен" in str(best["note"])

    def test_length_mismatch_is_an_error(self) -> None:
        samples = self._samples()[:2]
        with pytest.raises(ValueError):
            compute_metrics(samples, [0.5], 0.5)
