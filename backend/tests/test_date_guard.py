"""Защита от дат, которых нет в материалах дела."""

from __future__ import annotations

from datetime import date

from app.drafting.date_guard import (
    check_unsourced_dates,
    extract_dates,
    mark_unsourced_dates,
)


class TestExtract:
    def test_numeric_date(self) -> None:
        found = extract_dates("Договор от 12.03.2025")
        assert found == [("12.03.2025", date(2025, 3, 12))]

    def test_short_year(self) -> None:
        assert extract_dates("12.03.25") == [("12.03.25", date(2025, 3, 12))]

    def test_word_date(self) -> None:
        found = extract_dates("12 марта 2025 года")
        assert found == [("12 марта 2025 года", date(2025, 3, 12))]

    def test_word_date_with_g(self) -> None:
        found = extract_dates("5 мая 2026 г.")
        assert found == [("5 мая 2026 г.", date(2026, 5, 5))]

    def test_iso_date(self) -> None:
        assert extract_dates("2025-03-12") == [("2025-03-12", date(2025, 3, 12))]

    def test_invalid_date_ignored(self) -> None:
        assert extract_dates("31.02.2025") == []

    def test_no_dates(self) -> None:
        assert extract_dates("Ни одной даты в тексте") == []


class TestUnsourced:
    def test_date_not_in_materials_is_found(self) -> None:
        found = check_unsourced_dates(
            "Договор заключён 12.03.2025.",
            ["Договор поставки без даты."],
        )
        assert [item.value for item in found] == [date(2025, 3, 12)]

    def test_date_in_materials_is_not_flagged(self) -> None:
        found = check_unsourced_dates(
            "Договор заключён 12.03.2025.",
            ["Договор от 12.03.2025 № 7."],
        )
        assert found == []

    def test_word_form_matches_numeric_source(self) -> None:
        found = check_unsourced_dates(
            "Договор заключён 12 марта 2025 года.",
            ["Договор от 12.03.2025 № 7."],
        )
        assert found == []

    def test_known_dates_are_used(self) -> None:
        found = check_unsourced_dates(
            "Событие 01.02.2025.",
            ["Текст без дат."],
            known_dates=["01.02.2025"],
        )
        assert found == []

    def test_derived_deadline_near_marker_is_not_flagged(self) -> None:
        # 30 дней от известной даты рядом со словом «обжалование».
        found = check_unsourced_dates(
            "Решение может быть обжаловано в течение месяца, "
            "срок обжалования истекает 31.03.2025.",
            ["Событие от 01.03.2025."],
        )
        assert found == []

    def test_derived_offset_without_marker_is_flagged(self) -> None:
        found = check_unsourced_dates(
            "Событие произошло 31.03.2025.",
            ["Событие от 01.03.2025."],
        )
        assert [item.value for item in found] == [date(2025, 3, 31)]


class TestMarking:
    def test_marker_added_after_date(self) -> None:
        marked, found = mark_unsourced_dates(
            "Договор заключён 12.03.2025.",
            ["Материалы без даты."],
        )
        assert "12.03.2025 [дата отсутствует в материалах дела — проверьте]" in marked
        assert len(found) == 1

    def test_no_changes_when_all_dates_sourced(self) -> None:
        text = "Договор от 12.03.2025."
        marked, found = mark_unsourced_dates(text, [text])
        assert marked == text
        assert found == []
