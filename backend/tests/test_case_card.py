"""Извлечение номера дела и суда из текста материалов."""

from __future__ import annotations

from app.ingestion.case_card import extract_case_card


class TestNumber:
    def test_civil_case_number(self) -> None:
        number, _ = extract_case_card(["ИСКОВОЕ ЗАЯВЛЕНИЕ\nДело № 2-456/2025\n"])
        assert number == "2-456/2025"

    def test_arbitration_case_number(self) -> None:
        number, _ = extract_case_card(["Дело № А40-12345/2026"])
        assert number == "А40-12345/2026"

    def test_spaces_around_dash(self) -> None:
        number, _ = extract_case_card(["дело № 2 - 456 / 2025"])
        assert number == "2-456/2025"

    def test_no_number(self) -> None:
        number, _ = extract_case_card(["Просто текст без реквизитов"])
        assert number == ""


class TestCourt:
    def test_world_judge(self) -> None:
        text = "Мировой судья судебного участка № 123 района Хамовники г. Москвы 119034"
        _, court = extract_case_card([text])
        assert court == "Мировой судья судебного участка № 123 района Хамовники г. Москвы"

    def test_arbitration_court(self) -> None:
        _, court = extract_case_card(["В Арбитражный суд города Москвы\nИстец: ..."])
        assert court == "Арбитражный суд города Москвы"

    def test_district_court(self) -> None:
        _, court = extract_case_card(["Пресненский районный суд г. Москвы"])
        assert court.startswith("Пресненский районный суд")

    def test_no_court(self) -> None:
        _, court = extract_case_card(["Договор поставки № 7"])
        assert court == ""


class TestBoth:
    def test_takes_from_different_documents(self) -> None:
        number, court = extract_case_card([
            "Исковое заявление. Дело № 2-456/2025",
            "Ходатайство. Мировой судья судебного участка № 123",
        ])
        assert number == "2-456/2025"
        assert court.startswith("Мировой судья")

    def test_empty_texts(self) -> None:
        assert extract_case_card([]) == ("", "")
        assert extract_case_card(["", None]) == ("", "")  # type: ignore[list-item]
