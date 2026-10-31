"""Детерминированные валидаторы: суммы по назначению, даты, итоги.

Этап 3 ТЗ. Проверяется главное свойство: код ловит дефект сам, без
модели, и не выдаёт совпадение числа за подтверждение смысла.
"""

from __future__ import annotations

from decimal import Decimal

from app.verification.deterministic import (
    amounts_by_purpose,
    check_amounts_by_purpose,
    check_dates,
    check_error,
    check_total,
    missing_checks,
)

SOURCES = [
    "Истец просит взыскать долг 80 000 руб., проценты 9 122,19 руб. "
    "и госпошлину 4 000 руб. Договор займа заключён 10.02.2024. "
    "Ответчик передал 8 000 руб. платёжным поручением.",
]


class TestAmountsByPurpose:
    def test_purpose_is_detected(self) -> None:
        pairs = amounts_by_purpose("Взыскать долг 80 000 руб. и проценты 9 122,19 руб.")
        purposes = {purpose for purpose, _, _ in pairs}
        assert purposes == {"долг", "проценты"}

    def test_amount_without_purpose_is_skipped(self) -> None:
        # Назначение неизвестно — проверять нечего, угадывать нельзя.
        assert amounts_by_purpose("Уплачено 5 000 руб.") == []


class TestAmountsByPurposeCheck:
    def test_correct_amounts_pass(self) -> None:
        text = "Взыскать долг 80 000 руб., проценты 9 122,19 руб., пошлину 4 000 руб."
        assert check_amounts_by_purpose(text, SOURCES) == []

    def test_invented_amount(self) -> None:
        text = "Взыскать расходы на представителя 6 470 руб."
        found = check_amounts_by_purpose(text, SOURCES)
        assert [f.check_id for f in found] == ["AMOUNT_NOT_IN_SOURCES"]
        assert found[0].severity == "CRITICAL"

    def test_existing_amount_in_wrong_purpose(self) -> None:
        """Тест B ТЗ: 80 000 есть в деле, но это долг, а не проценты.

        Проверка «число встречается в материалах» такой дефект
        пропускает — именно поэтому назначение проверяется отдельно.
        """
        text = "Взыскать проценты 80 000 руб."
        found = check_amounts_by_purpose(text, SOURCES)
        assert [f.check_id for f in found] == ["AMOUNT_WRONG_PURPOSE"]
        assert found[0].severity == "HIGH"

    def test_payment_amount_is_allowed(self) -> None:
        text = "Ответчик передал 8 000 руб."
        assert check_amounts_by_purpose(text, SOURCES) == []


class TestDates:
    def test_date_from_materials(self) -> None:
        assert check_dates("Договор заключён 10.02.2024.", SOURCES) == []

    def test_invented_date(self) -> None:
        found = check_dates("Договор заключён 15.07.2025.", SOURCES)
        assert [f.check_id for f in found] == ["DATE_NOT_IN_SOURCES"]

    def test_word_date_is_recognised(self) -> None:
        sources = ["Договор от 10 февраля 2024 года."]
        assert check_dates("Событие 10.02.2024.", sources) == []


class TestTotal:
    def test_total_matches_parts(self) -> None:
        text = "Всего подлежит взысканию 93 122,19 руб."
        parts = [Decimal("80000"), Decimal("9122.19"), Decimal("4000")]
        assert check_total(text, parts) is None

    def test_total_mismatch(self) -> None:
        text = "Всего подлежит взысканию 100 000 руб."
        parts = [Decimal("80000"), Decimal("9122.19"), Decimal("4000")]
        found = check_total(text, parts)
        assert found is not None
        assert found.check_id == "TOTAL_MISMATCH"
        assert found.severity == "CRITICAL"

    def test_no_total_is_silent(self) -> None:
        assert check_total("Взыскать долг 80 000 руб.", [Decimal("80000")]) is None

    def test_ambiguous_total_is_silent(self) -> None:
        # Два числа рядом со словом «всего» — непонятно, какое итог.
        text = "Всего 10 000 руб. и 20 000 руб."
        assert check_total(text, [Decimal("30000")]) is None


class TestRequiredChecks:
    def test_missing_check_reported(self) -> None:
        found = missing_checks({"amounts"}, {"amounts", "dates"})
        assert [f.check_id for f in found] == ["CHECK_NOT_EXECUTED"]
        assert "dates" in found[0].fragment

    def test_all_checks_done(self) -> None:
        assert missing_checks({"amounts", "dates"}, {"amounts", "dates"}) == []


class TestTechnicalError:
    def test_error_is_separate_class(self) -> None:
        found = check_error("dates", RuntimeError("таймаут"))
        assert found.check_id == "CHECK_ERROR"
        assert "таймаут" in found.reason
