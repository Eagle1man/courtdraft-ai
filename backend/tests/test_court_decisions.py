"""Разбор решений из выгрузки sudrf: тело, вид дела, части, дата."""

from __future__ import annotations

from app.datasets.court_decisions import (
    case_kind,
    clean_decision,
    decision_date,
    extract_decision_body,
    split_sections,
)

PAGE = (
    "Javascript включить.  Карточка дела  2-146/2026  Судья Иванов  "
    "История заседаний 03.12.2025 06.02.2026  "
    "Именем Российской Федерации\n"
    "6 февраля 2026 года г. Нижний Новгород\n"
    "Суд установил обстоятельства дела.\n"
    "УСТАНОВИЛ:\n"
    "Истец обратился с требованием о взыскании долга.\n"
    "Суд приходит к выводу об удовлетворении иска.\n"
    "РЕШИЛ:\n"
    "Взыскать с ответчика 150 000 рублей.\n"
)


class TestBody:
    def test_takes_text_after_marker(self) -> None:
        body = extract_decision_body(PAGE)
        assert body.startswith("Именем Российской Федерации")
        assert "Карточка дела" not in body
        assert "Взыскать" in body

    def test_page_without_decision(self) -> None:
        assert extract_decision_body("Карточка дела и история заседаний") == ""

    def test_empty_text(self) -> None:
        assert extract_decision_body("") == ""

    def test_clean_collapses_spaces(self) -> None:
        cleaned = clean_decision("Текст   с   пробелами\u00a0и   переносами\n\n\n\nдальше")
        assert cleaned == "Текст с пробелами и переносами\n\nдальше"

    def test_clean_joins_letter_spaced_heading(self) -> None:
        # Разметка страницы разрывает заголовок по буквам.
        assert clean_decision("Р Е Ш И Л: взыскать долг") == "РЕШИЛ: взыскать долг"
        assert (
            clean_decision("У С Т А Н О В И Л:\nфакты")
            == "УСТАНОВИЛ:\nфакты"
        )


class TestDate:
    def test_date_from_first_line(self) -> None:
        body = extract_decision_body(PAGE)
        assert decision_date(body) == "2026-02-06"

    def test_no_date(self) -> None:
        assert decision_date("Именем Российской Федерации\nРЕШИЛ: взыскать") == ""


class TestCaseKind:
    def test_civil(self) -> None:
        assert case_kind("2-146/2026") == "civil"

    def test_administrative_claim(self) -> None:
        assert case_kind("2а-12/2026") == "administrative"

    def test_criminal(self) -> None:
        assert case_kind("1-18/2026") == "criminal"

    def test_administrative_offence(self) -> None:
        assert case_kind("5-94/2026") == "administrative_offence"

    def test_fallback_by_text(self) -> None:
        assert case_kind("б/н", "Дело рассматривается по КАС РФ") == "administrative"

    def test_unknown(self) -> None:
        assert case_kind("", "Текст без признаков") == "other"


class TestSections:
    def test_all_sections_found(self) -> None:
        body = extract_decision_body(PAGE)
        parts = split_sections(body)
        assert parts.descriptive.startswith("Именем Российской Федерации")
        assert "УСТАНОВИЛ" not in parts.descriptive
        assert "Истец обратился" in parts.established
        assert "приходит к выводу" in parts.established
        assert "Взыскать" in parts.operative
        assert not parts.operative.startswith(":")

    def test_no_sections(self) -> None:
        parts = split_sections("Просто текст без заголовков")
        assert parts.descriptive == "Просто текст без заголовков"
        assert parts.established == ""
        assert parts.operative == ""

    def test_empty(self) -> None:
        parts = split_sections("")
        assert (parts.descriptive, parts.established, parts.operative) == ("", "", "")
