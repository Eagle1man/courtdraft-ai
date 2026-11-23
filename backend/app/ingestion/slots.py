"""Типизированные слоты: значения полей только из строк-полей одного документа."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.money import table_units  # реэкспорт: приёмка импортирует отсюда

__all__ = ["SlotResult", "validate_address", "validate_court", "extract_slots", "table_units"]

_MARKER = re.compile(r"^\[[^\]]+ — по данным дела\]$")
_FIELD_MAP = {
    "истец": "plaintiff.name",
    "ответчик": "defendant.name",
    "адрес истца": "plaintiff.address",
    "адрес ответчика": "defendant.address",
    "телефон истца": "plaintiff.phone",
    "идентификатор истца": "plaintiff.identifier",
    "идентификатор ответчика": "defendant.identifier",
    "суд (для шапки)": "case.court_for_header",
    "город": "case.city",
    "дата иска": "case.suit_date",
}
_STOP = (
    "адрес почтового отделения", "адресат", "адрес суда", "отправитель",
    "сумма почтового сбора", "вес отправления", "отметка",
)
_LINE = re.compile(r"^[ \t]*([А-Яа-яЁё ()]{3,40}?)[ \t]*:[ \t]*(.*)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SlotResult:
    status: str  # OK | MISSING | INVALID
    value: str = ""


def validate_address(value: str) -> SlotResult:
    text = (value or "").strip()
    if _MARKER.match(text):
        return SlotResult("MISSING", text)
    if not text or len(text) < 10 or len(text) > 200 or "\n" in text:
        return SlotResult("INVALID", text)
    low = text.lower()
    if any(bad in low for bad in ("|", "таблица", "рпо", "подтверждается", "листов")):
        return SlotResult("INVALID", text)
    has_city = bool(re.search(r"\b(?:г\.|гор\.|город|пос\.|с\.|дер\.)", low))
    has_street = bool(re.search(r"\b(?:ул\.|улица|пр-т|просп|пер\.|д\.|дом|кв\.)", low))
    if has_city and has_street and re.search(r"\d", text):
        return SlotResult("OK", text)
    return SlotResult("INVALID", text)


def validate_court(value: str) -> SlotResult:
    text = (value or "").strip()
    if not text:
        return SlotResult("MISSING", text)
    if any(bad in text for bad in ("[", "]", "___", "уточняется", "TBD")):
        return SlotResult("INVALID", text)
    if re.search(r"суд|судье|участок", text, re.IGNORECASE):
        return SlotResult("OK", text)
    return SlotResult("INVALID", text)


def extract_slots(text: str, *, document_id: str = "") -> dict[str, str]:
    """Поля только по '^Имя поля:' в пределах переданного документа."""
    out: dict[str, str] = {}
    for match in _LINE.finditer(text or ""):
        key = " ".join(match.group(1).lower().split())
        if any(key.startswith(stop) for stop in _STOP):
            continue
        slot = _FIELD_MAP.get(key)
        if slot is None:
            continue
        value = match.group(2).strip()
        if value and slot not in out:
            out[slot] = value
    return out
