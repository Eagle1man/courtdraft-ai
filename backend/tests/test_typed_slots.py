"""Регрессия P0-04: адреса и поля только из типизированных слотов."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "value",
    [
        "Таблица 1: | № | Наименование документа | Листов | | 1 | Копия договора | 1 |",
        "ату 30.12.2024, что подтверждается отчётом об отслеживании почтовых отправлений",
        "РПО № 10500012345678 от 23.12.2024",
        "x",
    ],
)
def test_address_rejects_garbage(value: str) -> None:
    from app.ingestion.slots import validate_address

    assert validate_address(value).status != "OK"


def test_address_marker_is_missing_not_value() -> None:
    from app.ingestion.slots import validate_address

    assert validate_address("[адрес истца — по данным дела]").status == "MISSING"


def test_address_stop_list() -> None:
    from app.ingestion.slots import extract_slots

    text = (
        "Истец: Смирнова Ольга Викторовна\n"
        "Адрес истца: [адрес истца — по данным дела]\n"
        "Адрес почтового отделения: [адрес почтового отделения — по данным дела]\n"
        "Адресат: Кузнецов Андрей Павлович, [адрес ответчика — по данным дела]\n"
    )
    slots = extract_slots(text, document_id="09")
    assert "почтов" not in str(slots.get("plaintiff.address", ""))


def test_no_cross_document_capture() -> None:
    from app.ingestion.slots import extract_slots

    doc_a = "Истец: Смирнова Ольга Викторовна\n"
    doc_b = "| № | Наименование документа | Листов |\n| 1 | Копия договора | 1 |\n"
    slots = extract_slots(doc_a, document_id="A")
    other = extract_slots(doc_b, document_id="B")
    assert "Наименование" not in str(slots)
    assert "Смирнова" not in str(other)


def test_no_500_char_slice() -> None:
    """Значение поля не обрезается ради «валидации»."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "drafting" / "claim_suggest.py"
    ).read_text(encoding="utf-8")
    assert "address[:500]" not in source
    assert "name[:300]" not in source


def test_court_rejects_placeholder() -> None:
    from app.ingestion.slots import validate_court

    assert validate_court("Ленинский районный суд г. [наименование города]").status != "OK"
    assert validate_court("Мировому судье судебного участка № 5 Центрального района г. Москвы").status == "OK"
