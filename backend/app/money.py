"""Единый канон денег: целые копейки, четыре прохода разбора, один формат вывода.

Запрещено вне этого модуля: float для денег, %g, самописные regex для сумм.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from app.money_words import capitalize_words, plural, rubles_to_words, words_equal

Kopecks = int
UnitSource = Literal["composite", "currency", "typed_field", "table_column", "kopecks_only"]

_SP = " \u00a0\u2009\u202f"
_NUM = rf"\d[\d{_SP}]*"

_RE_COMPOSITE = re.compile(
    rf"({_NUM}(?:,\d{{2}})?)\s*\(([^)]{{3,120}})\)\s*рубл\w*\s*(\d{{1,2}})\s*копе\w*",
    re.IGNORECASE,
)
_RE_CURRENCY = re.compile(
    rf"(?<![\d.,])({_NUM}(?:[.,]\d{{1,2}})?)(?![\d.,])\s*(?:руб(?:\.|лей|ля|ль|\b)|₽|р\.)",
    re.IGNORECASE,
)
_RE_KOPECKS = re.compile(r"(?<![\d.,])(\d{1,2})\s*коп\w*", re.IGNORECASE)
_RE_TYPED = re.compile(
    r"^[ \t]*(?:Основной долг|Долг|Сумма долга|Остаток(?: основного)?(?: долга)?|"
    r"Цена иска|Госпошлина(?: уплачена)?|Государственная пошлина(?: уплачена)?|"
    r"Итого(?: к взысканию)?(?: процентов)?|Всего(?: к взысканию)?|Сумма)"
    r"[ \t]*:[ \t]*(" + _NUM + r"(?:[.,]\d{1,2})?)",
    re.IGNORECASE | re.MULTILINE,
)
_RE_TABLE_LINE = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$", re.MULTILINE)

_RE_DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b")
_RE_REF = re.compile(
    r"\b(?:ст|стать[яи]\w*|п|пп|ч|част[ьи]\w*|абз)\.?\s*\d+(?:\.\d+)*"
    r"|№\s*[\w./-]+|\b(?:ИНН|ОГРНИП|ОГРН|КПП|КБК|ОКТМО|СНИЛС|РПО)\s*[\d\s-]{5,}"
    r"|\b(?:19|20)\d{2}\s*(?:год\w*|г\.)?",
    re.IGNORECASE,
)
_RE_LONG_ID = re.compile(r"(?<!\d)\d{8,}(?!\d)")


@dataclass(frozen=True, slots=True)
class MoneyToken:
    kopecks: Kopecks
    raw: str
    start: int
    end: int
    unit_source: UnitSource
    document_id: str = ""
    confidence: float = 1.0


def _clean_int(text: str) -> str:
    return re.sub(rf"[{_SP}]", "", text)


def to_kopecks(whole: str, fraction: str | None = None) -> Kopecks:
    digits = _clean_int(whole).replace(",", ".")
    value = Decimal(digits)
    if fraction:
        value += Decimal(f"0.{fraction}")
    return int((value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100).to_integral_value())


def _split_number(raw: str) -> tuple[str, str | None]:
    text = raw.strip()
    if "," in text:
        whole, frac = text.rsplit(",", 1)
        return whole, frac
    if "." in text and len(text.rsplit(".", 1)[1]) <= 2:
        whole, frac = text.rsplit(".", 1)
        return whole, frac
    return text, None


def _excluded_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _RE_DATE.finditer(text)]
    spans += [m.span() for m in _RE_REF.finditer(text)]
    spans += [m.span() for m in _RE_LONG_ID.finditer(text)]
    return spans


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def table_units(text: str) -> dict[int, str]:
    """Единицы колонок pipe-таблицы: индекс колонки -> 'RUB'."""
    units: dict[int, str] = {}
    for match in _RE_TABLE_LINE.finditer(text or ""):
        cells = [c.strip() for c in match.group(1).split("|")]
        if not any("руб" in c.lower() for c in cells):
            continue
        if any(re.search(r"\d{2}\.\d{2}\.\d{4}", c) for c in cells):
            continue
        for index, cell in enumerate(cells):
            if re.search(r",\s*руб\.?", cell, re.IGNORECASE):
                units[index] = "RUB"
        if units:
            break
    return units


def parse_money(
    text: str,
    *,
    document_id: str = "",
    typed_fields: dict[str, str] | None = None,
    table_units_map: dict[int, str] | None = None,
) -> list[MoneyToken]:
    """Деньги текста. Проходы: составная форма → валюта → поле → колонка → копейки."""
    source = text or ""
    excluded = _excluded_spans(source)
    tokens: list[MoneyToken] = []
    taken: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(a < y and x < b for x, y in taken)

    # A. Составная форма «X (пропись) рубля NN копеек» — высший приоритет.
    for m in _RE_COMPOSITE.finditer(source):
        whole, frac = _split_number(m.group(1))
        value = to_kopecks(whole, "0") + int(m.group(3))
        confidence = 1.0 if words_equal(value // 100, m.group(2)) else 0.4
        tokens.append(MoneyToken(value, m.group(0), m.start(), m.end(),
                                 "composite", document_id, confidence))
        taken.append(m.span())

    # B. Цифровая форма с валютой.
    for m in _RE_CURRENCY.finditer(source):
        if overlaps(*m.span()) or _inside(m.start(), excluded):
            continue
        if re.match(r"\s*коп", source[m.end():m.end() + 12], re.IGNORECASE):
            continue
        whole, frac = _split_number(m.group(1))
        tokens.append(MoneyToken(to_kopecks(whole, frac), m.group(0), m.start(), m.end(),
                                 "currency", document_id))
        taken.append(m.span())

    # C. Типизированное поле без валюты: ПЕРВОЕ число после двоеточия.
    for m in _RE_TYPED.finditer(source):
        start = m.start(1)
        if overlaps(start, m.end(1)) or _inside(start, excluded):
            continue
        whole, frac = _split_number(m.group(1))
        tokens.append(MoneyToken(to_kopecks(whole, frac), m.group(1), start, m.end(1),
                                 "typed_field", document_id))
        taken.append((start, m.end(1)))

    # D. Ячейки таблицы наследуют единицу из заголовка колонки.
    units = table_units_map if table_units_map is not None else table_units(source)
    if units:
        for line in _RE_TABLE_LINE.finditer(source):
            cells = line.group(1).split("|")
            offset = line.start(1)
            cursor = offset
            for index, cell in enumerate(cells):
                start_cell = cursor
                cursor += len(cell) + 1
                if units.get(index) != "RUB":
                    continue
                number = re.search(rf"(?<![\d.,])({_NUM}(?:,\d{{1,2}})?)(?![\d.,])", cell)
                if not number:
                    continue
                a, b = start_cell + number.start(1), start_cell + number.end(1)
                if overlaps(a, b):
                    continue
                whole, frac = _split_number(number.group(1))
                tokens.append(MoneyToken(to_kopecks(whole, frac), number.group(1), a, b,
                                         "table_column", document_id))
                taken.append((a, b))

    # E. Только копейки, не входящие в составную форму.
    for m in _RE_KOPECKS.finditer(source):
        if overlaps(*m.span()) or _inside(m.start(), excluded):
            continue
        tokens.append(MoneyToken(int(m.group(1)), m.group(0), m.start(), m.end(),
                                 "kopecks_only", document_id))
        taken.append(m.span())

    tokens.sort(key=lambda t: t.start)
    return [t for t in tokens if 0 < t.kopecks <= 10**12]


_RUB_FORMS = ("рубль", "рубля", "рублей")
_KOP_FORMS = ("копейка", "копейки", "копеек")


def format_rub(kop: Kopecks, *, style: Literal["plain", "words", "short"] = "plain") -> str:
    """plain: '90 000,00 руб.'; words: '90 000 (Девяносто тысяч) рублей 00 копеек'."""
    if kop < 0:
        raise ValueError("отрицательная сумма")
    rubles, kopecks = divmod(kop, 100)
    grouped = f"{rubles:,}".replace(",", " ")
    if style == "short":
        return f"{grouped},{kopecks:02d}"
    if style == "plain":
        return f"{grouped},{kopecks:02d} руб."
    words = capitalize_words(rubles_to_words(rubles))
    return (f"{grouped} ({words}) {plural(rubles, _RUB_FORMS)} "
            f"{kopecks:02d} {plural(kopecks, _KOP_FORMS)}")
