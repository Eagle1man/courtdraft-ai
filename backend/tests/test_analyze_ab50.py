"""Разбор артефактов A/B: снятие дата-ложных P0 и метрики сверки.

Тесты держат механику, а не цифры конкретного прогона: сохранённые
issues пересчитываются без вызова модели, поэтому ошибка здесь тихо
улучшила бы отчёт.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найден скрипт {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


repository = _load("analyze_ab50_artifacts", "analyze_ab50_artifacts.py")


def _case(
    draft_text: str,
    issues: list[dict[str, Any]],
    p0: list[str] | None = None,
    operative: str = "",
) -> dict[str, Any]:
    return {
        "case": "case-001",
        "draft_text": draft_text,
        "operative_text": operative,
        "p0": p0 or [],
        "verification": {"issues": issues},
    }


def _unsupported(reason: str, section: str = "Мотивировочная часть") -> dict[str, Any]:
    return {
        "type": "UNSUPPORTED_CLAIM",
        "severity": "CRITICAL",
        "text": "фрагмент проекта",
        "reason": reason,
        "section": section,
    }


class TestDateFalsePositive:
    def test_plenum_date_is_removed(self) -> None:
        case = _case(
            "В силу п. 48 постановления Пленума ВС РФ от 24.03.2016 № 7.",
            [_unsupported("число 2016 отсутствует в материалах дела")],
            ["CRITICAL UNSUPPORTED_CLAIM: фрагмент проекта"],
        )
        kept, removed, kept_issues = repository.recompute_p0(case)
        assert kept == []
        assert len(removed) == 1
        assert kept_issues == []

    def test_written_date_is_removed(self) -> None:
        case = _case(
            "Суд руководствуется разъяснениями от 24 марта 2016 года.",
            [_unsupported("число 2016 отсутствует в материалах дела")],
        )
        kept, removed, _ = repository.recompute_p0(case)
        assert kept == []
        assert len(removed) == 1

    def test_real_invented_amount_is_kept(self) -> None:
        case = _case(
            "Взыскать 999 999 рублей.",
            [_unsupported("число 999999 отсутствует в материалах дела")],
        )
        kept, removed, kept_issues = repository.recompute_p0(case)
        assert len(kept) == 1
        assert removed == []
        assert len(kept_issues) == 1

    def test_mixed_numbers_keep_the_issue(self) -> None:
        case = _case(
            "Постановление от 24.03.2016 № 7; взыскать 5000 рублей.",
            [
                _unsupported(
                    "число 2016 отсутствует; число 5000 отсутствует в материалах"
                )
            ],
        )
        kept, removed, _ = repository.recompute_p0(case)
        assert len(kept) == 1
        assert removed == []

    def test_non_unsupported_issue_is_not_filtered(self) -> None:
        issue = {
            "type": "CONTRADICTION",
            "severity": "CRITICAL",
            "text": "суд не совпадает",
            "reason": "число 2016 отсутствует в материалах дела",
            "section": "Вводная часть",
        }
        assert not repository.date_only_issue(issue, "от 24.03.2016")


class TestGrouping:
    def test_type_and_section_are_counted(self) -> None:
        issues = [
            {
                "type": "CONTRADICTION",
                "severity": "CRITICAL",
                "text": "a",
                "section": "Вводная часть",
            },
            {
                "type": "CONTRADICTION",
                "severity": "CRITICAL",
                "text": "b",
                "section": "Вводная часть",
            },
            {
                "type": "UNSUPPORTED_CLAIM",
                "severity": "CRITICAL",
                "text": "c",
                "section": "Мотивировочная часть",
            },
        ]
        counter = repository.p0_by_type_section(issues)
        assert counter[("CONTRADICTION — противоречие", "Вводная часть")] == 2
        assert counter[("UNSUPPORTED_CLAIM — неподтверждённое число/факт", "Мотивировочная часть")] == 1

    def test_duplicate_issue_is_counted_once(self) -> None:
        issue = {
            "type": "CONTRADICTION",
            "severity": "CRITICAL",
            "text": "одно и то же",
            "section": "Вводная часть",
        }
        case = _case("текст", [issue, dict(issue)])
        _, _, kept_issues = repository.recompute_p0(case)
        assert len(kept_issues) == 1


class TestCitationMetrics:
    def test_precision_recall_f1(self) -> None:
        case = {"draft_text": "ст. 98 ГПК РФ и ст. 103 ГПК РФ"}
        reference = "ст. 98 ГПК РФ и ст. 198 ГПК РФ"
        metrics = repository.citation_metrics(case, reference)
        assert metrics["both_count"] == 1
        assert metrics["system_count"] == 2
        assert metrics["court_count"] == 2
        assert metrics["precision"] == 0.5
        assert metrics["recall"] == 0.5
        assert metrics["f1"] == 0.5

    def test_empty_reference_has_zero_f1(self) -> None:
        metrics = repository.citation_metrics({"draft_text": "ст. 98 ГПК РФ"}, "")
        assert metrics["f1"] == 0.0


class TestAmounts:
    def test_parenthetical_total_counts(self) -> None:
        case = {"operative_text": "Взыскать 46 940 руб."}
        reference = (
            "45 379 руб., в том числе 25 000 руб., итого "
            "46 940 (сорок шесть тысяч девятьсот сорок) руб."
        )
        metrics = repository.amount_metrics(case, reference)
        assert metrics["court_unique"] == [25000, 45379, 46940]
        assert metrics["found"] == [46940]
        assert metrics["recall"] == 0.3333
        assert metrics["precision"] == 1.0

    def test_kopeck_fragment_is_not_a_sum(self) -> None:
        case = {"operative_text": "Взыскать 1 561, 37 руб."}
        metrics = repository.amount_metrics(case, "1 561, 37 руб.")
        assert metrics["court_unique"] == [1561]
        assert metrics["found"] == [1561]
        assert metrics["f1"] == 1.0
