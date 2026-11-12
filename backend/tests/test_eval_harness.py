"""Оценочная ведомость для юриста (п. 78 разбора рецензента).

Harness собирает три графы — исход, суммы, нормы, — и не оценивает сам.
Тесты держат именно механику: грубые маркеры исхода, извлечение статей,
рублёвых сумм и склейку ведомости. Скрипт лежит вне backend/, поэтому
грузится через importlib — как его грузит build_real_cases.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найден скрипт {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


harness = _load("eval_harness", "eval_harness.py")


class TestOutcomeMarkers:
    """Маркеры исхода — корни слов, а не точные фразы."""

    def test_partial_satisfaction(self) -> None:
        text = "Удовлетворить исковые требования истца частично, в остальной части отказать."
        found = harness.outcome_of(text)
        assert "частичное удовлетворение" in found
        assert "отказ (в части требований)" in found

    def test_full_satisfaction(self) -> None:
        assert "полное удовлетворение" in harness.outcome_of(
            "Исковые требования истца удовлетворить в полном объёме."
        )

    def test_denial(self) -> None:
        assert "отказ в иске" in harness.outcome_of(
            "В удовлетворении исковых требований оставить без удовлетворения."
        )

    def test_unparsed_is_empty(self) -> None:
        # Неготовный текст не получает маркеров — в ведомости это
        # показывается как «не разобрано», а не подменяется угаданным.
        assert harness.outcome_of("ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ") == []


class TestLaws:
    """Статьи: «ст. 98 ГПК РФ», «ст.333.19 НК РФ», «ч. 1 ст. 320-322 ГПК РФ»."""

    def test_basic_extraction(self) -> None:
        laws = harness.laws_in("на основании ст. 98 ГПК РФ взыскать расходы")
        assert "98 ГПК РФ" in laws

    def test_dotted_number_and_mixed_case_code(self) -> None:
        laws = harness.laws_in("штраф по ст. 333.19 НК РФ и ст. 12.15 КоАП РФ")
        assert "333.19 НК РФ" in laws
        assert "12.15 КОАП РФ" in laws

    def test_leading_part_and_range(self) -> None:
        laws = harness.laws_in("ч. 1 ст. 320-322 ГПК РФ не применяется")
        assert {"320 ГПК РФ", "321 ГПК РФ", "322 ГПК РФ"} <= set(laws)

    def test_range_dash_and_bounds_do_not_break_overlap(self) -> None:
        # Суд: «194-198», проект: «194–199». Это пересечение статей,
        # а не разные нормы из-за типа тире.
        system = harness.laws_in("ст. 194–199 ГПК РФ")
        court = harness.laws_in("ст. 194-198 ГПК РФ")
        assert len(system.keys() & court.keys()) == 5

    def test_duplicate_mentions_collapse(self) -> None:
        laws = harness.laws_in("ст. 98 ГПК РФ, а также ст. 98 ГПК РФ")
        assert list(laws.values()).count("ст. 98 ГПК РФ") == 1

    def test_diff_three_sets(self) -> None:
        system = harness.laws_in("ст. 98 ГПК РФ, ст. 103 ГПК РФ")
        court = harness.laws_in("ст. 98 ГПК РФ, ст. 198 ГПК РФ")
        diff = harness.laws_diff(system, court)
        assert diff["both"] == ["ст. 98 ГПК РФ"]
        assert diff["system_only"] == ["ст. 103 ГПК РФ"]
        assert diff["court_only"] == ["ст. 198 ГПК РФ"]

    def test_full_code_name_is_normalized(self) -> None:
        # Суд пишет «Гражданского кодекса РФ», проект — «ГК РФ».
        # Без нормализации это две разные нормы и ложный ноль в F1.
        laws = harness.laws_in("ст. 548 Гражданского кодекса РФ")
        assert "548 ГК РФ" in laws

    def test_full_procedural_code_is_normalized(self) -> None:
        laws = harness.laws_in(
            "ст. 194 Гражданского процессуального кодекса РФ"
        )
        assert "194 ГПК РФ" in laws

    def test_multi_article_list(self) -> None:
        laws = harness.laws_in("ст. 56, 67 ГПК РФ")
        assert set(laws) == {"56 ГПК РФ", "67 ГПК РФ"}

    def test_article_conjunction_list(self) -> None:
        laws = harness.laws_in("статьями 309, 310 и 314 ГК РФ")
        assert set(laws) == {"309 ГК РФ", "310 ГК РФ", "314 ГК РФ"}

    def test_housing_code_is_recognized(self) -> None:
        laws = harness.laws_in("ст. 155 Жилищного кодекса РФ")
        assert "155 ЖК РФ" in laws

    def test_reference_without_code_is_skipped(self) -> None:
        # «ст. 5 Закона» — не кодекс; выдумывать кодекс нельзя.
        assert harness.laws_in("ст. 5 Закона") == {}


class TestRubAmounts:
    """Рублёвые суммы: целая часть, копейки — только в контексте."""

    def test_spaced_thousands(self) -> None:
        amounts = harness.rub_amounts("взыскать 11 551 руб. с ответчика")
        assert amounts and amounts[0][0] == 11551

    def test_decimal_keeps_integer_part(self) -> None:
        # «11551,84 руб.» раньше давало 1155184 — вся строка цифр
        # склеивалась вместе с копейками.
        amounts = harness.rub_amounts("взыскать 11551,84 руб.")
        assert amounts and amounts[0][0] == 11551

    def test_kopecks_not_counted_as_rubles(self) -> None:
        amounts = harness.rub_amounts("739 рублей 60 копеек")
        assert [value for value, _context in amounts] == [739]

    def test_context_is_attached(self) -> None:
        amounts = harness.rub_amounts("госпошлину в размере 444 руб. уплачен")
        assert amounts and "444" in amounts[0][1]

    def test_comma_space_kopecks_keep_rubles(self) -> None:
        # В реальных решениях встречается «1 561, 37 руб.»: пробел после
        # запятой не должен превращать копейки в самостоятельную сумму.
        amounts = harness.rub_amounts("госпошлина в размере 1 561, 37 руб.")
        assert [value for value, _context in amounts] == [1561]

    def test_parenthetical_total(self) -> None:
        # «46 940 (сорок шесть тысяч девятьсот сорок) руб.» — итог
        # с расшифровкой; раньше он выпадал из сверки.
        amounts = harness.rub_amounts(
            "итого 46 940 (сорок шесть тысяч девятьсот сорок) руб."
        )
        assert [value for value, _context in amounts] == [46940]

    def test_kopeck_fragment_not_a_separate_amount(self) -> None:
        amounts = harness.rub_amounts("1 561, 37 руб.")
        assert 37 not in [value for value, _context in amounts]


class TestAmountMetrics:
    """Сверка по множествам: повторы не завышают знаменатель."""

    def test_duplicates_collapse(self) -> None:
        court = [(45379, ""), (25000, ""), (45379, "")]
        system = [(25000, ""), (45379, ""), (45379, "")]
        metrics = harness.amount_metrics(court, system)
        assert metrics["court_count"] == 2
        assert metrics["system_count"] == 2
        assert metrics["f1"] == 1.0
        assert metrics["full_coverage"]

    def test_missing_sum_lowers_recall(self) -> None:
        metrics = harness.amount_metrics([(100, ""), (200, "")], [(100, "")])
        assert metrics["recall"] == 0.5
        assert metrics["precision"] == 1.0
        assert metrics["f1"] == 0.6667
        assert not metrics["full_coverage"]

    def test_empty_court_has_zero_metrics(self) -> None:
        metrics = harness.amount_metrics([], [(100, "")])
        assert metrics["recall"] == 0.0
        assert metrics["precision"] == 0.0
        assert metrics["f1"] == 0.0


class TestFoundIn:
    def test_exact_and_rounding(self) -> None:
        court = [(11551, "…11 551 руб…")]
        assert harness.found_in(11551.0, court)
        assert harness.found_in(11551.84, court)  # копейки — в пределах 1 руб.

    def test_missing(self) -> None:
        assert not harness.found_in(11551.0, [(472, "…472 рубля…")])


def _processed() -> dict[str, Any]:
    """Срез, который process() реально отдаёт (см. build_real_site)."""
    return {
        "rules": {
            "claims": [
                {
                    "amount": 11107.0,
                    "basis": "задолженность по договору",
                    "source": "01_claim.txt",
                }
            ],
            "fee_paid": 444,
            "total": 11107.0,
            "total_with_fee": 11551.0,
        },
        "operative_text": (
            "Удовлетворить исковые требования истца в полном объёме. "
            "Взыскать 11 551 руб., в том числе 444 руб. госпошлины."
        ),
        "verification": {
            "status": "REVIEW_REQUIRED",
            "issues": [
                {
                    "type": "MONEY_MISMATCH",
                    "severity": "significant",
                    "reason": "сумма в резолютивной не совпала с требованием",
                    "section": "operative",
                }
            ],
            "unsupported_claims": 1,
        },
    }


class TestCaseBlock:
    MANIFEST = {
        "category": "взыскание долга",
        "claim_amount": 11107.0,
        "documents": [{"filename": "01_claim.txt"}],
    }
    COURT = (
        "Удовлетворить исковые требования частично. Взыскать 472 рубля. "
        "В остальной части отказать. На основании ст. 98 ГПК РФ."
    )

    def _block(self) -> str:
        return harness.case_block("real-01", self.MANIFEST, _processed(), self.COURT)

    def test_two_sides_of_outcome(self) -> None:
        block = self._block()
        assert "система: полное удовлетворение" in block
        assert "суд: частичное удовлетворение" in block

    def test_amounts_and_number_check(self) -> None:
        block = self._block()
        assert "итог 11107.00 руб. (с пошлиной 11551.00 руб.)" in block
        assert "472 руб." in block  # сумма суда с контекстом
        # Итога 11551 среди сумм суда (472) нет — это факт, не оценка.
        assert "не найден" in block

    def test_law_asymmetry_visible(self) -> None:
        block = self._block()
        assert "только у суда: ст. 98 ГПК РФ" in block

    def test_verification_summary_present(self) -> None:
        block = self._block()
        assert "REVIEW_REQUIRED" in block
        assert "утверждений без источника: 1" in block

    def test_verdict_fields_empty(self) -> None:
        # Ведомость ничего не заключает за оценщика.
        block = self._block()
        assert block.count("- [ ]") >= 9
        assert "### Вывод оценщика" in block

    def test_needs_review_note_when_unparsed(self) -> None:
        processed = _processed()
        processed["operative_text"] = "ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ: иск не разобран."
        block = harness.case_block(
            "real-02", self.MANIFEST, processed, "удовлетворить частично."
        )
        assert "не разобрано (пометка о неготовности)" in block
