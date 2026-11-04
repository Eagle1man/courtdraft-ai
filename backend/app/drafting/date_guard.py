"""Проверка дат в сгенерированном тексте по материалам дела.

Зачем отдельный модуль: на живом тесте 8 из 26 проектов содержали даты,
которых в материалах нет. Модель «дорисовывает» дату договора, передачи
денег или решения. Проверка утверждений ловит это как неподтверждённое
утверждение, но уже после того, как дата попала в акт.

Правило то же, что для сумм (app.drafting.numeric_guard): дата из текста
либо встречается в материалах дела, либо помечается прямо в тексте.
Удалять нельзя — дата может быть верной, но из незагруженного документа.

Исключение — вычисляемые даты: срок обжалования, дата вступления в силу.
Суд не берёт их из материалов, а считает от известной даты. Такая дата
объяснима, если отличается от известной на стандартный срок и стоит
рядом со словом «обжалование», «вступает», «срок», «не позднее».
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

#: Числовые даты: 12.03.2025, 12/03/2025, 12.03.25.
_NUMERIC_DATE = re.compile(
    r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})(?!\d)"
)

#: Даты словами: «12 марта 2025 г.», «12 марта 2025 года».
_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}

_WORD_DATE = re.compile(
    r"(?<!\d)(\d{1,2})\s+"
    r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|"
    r"август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
    r"\s+(\d{4})(?:\s*(?:г\.|года))?",
    re.IGNORECASE,
)

#: Даты ISO: 2025-03-12.
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

#: Стандартные сроки, от которых дата может быть вычислена.
_DERIVED_OFFSETS = (1, 3, 5, 7, 10, 14, 15, 20, 30, 60, 90)

#: Слова рядом с вычисляемой датой.
_DERIVED_MARKERS = (
    "обжалован",
    "вступ",
    "вступа",
    "истек",
    "истека",
    "не позднее",
    "срок",
    "течение",
)

_WINDOW = 120


@dataclass(frozen=True)
class UnsourcedDate:
    """Дата, которой нет в материалах дела."""

    raw: str
    value: date


def _month_number(word: str) -> int:
    lowered = word.lower().replace("ё", "е")
    # «март» начинается с «ма», поэтому проверяется до мая.
    if lowered.startswith("мар"):
        return 3
    for prefix, number in _MONTHS.items():
        if lowered.startswith(prefix):
            return number
    return 0


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_dates(text: str) -> list[tuple[str, date]]:
    """Найти даты в тексте. Возвращает (как написано, значение)."""
    found: list[tuple[str, date]] = []
    source = text or ""

    for match in _NUMERIC_DATE.finditer(source):
        day, month, year = match.groups()
        full_year = int(year) + (2000 if len(year) == 2 else 0)
        value = _safe_date(full_year, int(month), int(day))
        if value is not None:
            found.append((match.group(0), value))

    for match in _WORD_DATE.finditer(source):
        day, month_word, year = match.groups()
        month = _month_number(month_word)
        value = _safe_date(int(year), month, int(day)) if month else None
        if value is not None:
            found.append((match.group(0), value))

    for match in _ISO_DATE.finditer(source):
        year, month, day = match.groups()
        value = _safe_date(int(year), int(month), int(day))
        if value is not None:
            found.append((match.group(0), value))

    return found


def _source_dates(texts: Iterable[str], known_dates: Iterable[str]) -> set[date]:
    values: set[date] = set()
    for text in texts:
        values.update(value for _raw, value in extract_dates(text or ""))
    for raw in known_dates:
        values.update(value for _raw, value in extract_dates(raw or ""))
    return values


def _near_marker(text: str, start: int) -> bool:
    window = (text[max(0, start - _WINDOW) : start + _WINDOW] or "").lower()
    return any(marker in window for marker in _DERIVED_MARKERS)


def _is_derived(value: date, known: set[date]) -> bool:
    """Может ли дата быть вычислена из известной стандартным сроком."""
    for base in known:
        for offset in _DERIVED_OFFSETS:
            if value in (base + timedelta(days=offset), base - timedelta(days=offset)):
                return True
    return False


def check_unsourced_dates(
    text: str,
    source_documents: Iterable[str],
    known_dates: Iterable[str] = (),
) -> list[UnsourcedDate]:
    """Даты из текста, которых нет ни в одном документе дела."""
    known = _source_dates(source_documents, known_dates)
    result: list[UnsourcedDate] = []
    seen: set[date] = set()

    for raw, value in extract_dates(text or ""):
        if value in known or value in seen:
            continue
        seen.add(value)
        # Срок, отсчитанный от известной даты, — арифметика, а не выдумка.
        if _is_derived(value, known) and _near_marker(text, text.find(raw)):
            continue
        result.append(UnsourcedDate(raw=raw, value=value))
    return result


def mark_unsourced_dates(
    text: str,
    source_documents: Iterable[str],
    known_dates: Iterable[str] = (),
    marker: str = "[дата отсутствует в материалах дела — проверьте]",
) -> tuple[str, list[UnsourcedDate]]:
    """Пометить в тексте даты, которых нет в материалах дела.

    Удалять дату нельзя: она может быть верной, но взятой из документа,
    который не загрузили. Задача — не пропустить её в акт молча.
    """
    unsourced = check_unsourced_dates(text, source_documents, known_dates)
    if not unsourced:
        return text, []

    marked = text
    for item in unsourced:
        pattern = re.compile(re.escape(item.raw))
        marked = pattern.sub(lambda m: f"{m.group(0)} {marker}", marked, count=1)
    return marked, unsourced
