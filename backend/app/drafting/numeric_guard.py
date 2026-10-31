"""Проверка денежных сумм в сгенерированном тексте по материалам дела.

Зачем отдельный модуль: модель периодически «дописывает» суммы, которых
в деле нет (живой прогон: 6 470 руб. судебных расходов при том, что
истец просил только долг, проценты и госпошлину 4 000 руб.). Проверка
утверждений (app.verification) ловит это как неподтверждённое
утверждение, но уже после того, как сумма попала в текст акта.

Правило простое и проверяемое: сумма из текста либо встречается в
материалах дела, либо помечается прямо в тексте. Никаких «наверное,
это итог» — итог тоже должен быть суммой известных слагаемых, и только
тогда он считается подтверждённым.

Модуль намеренно не знает про разделы и модели: на вход текст и
исходники, на выход текст с пометками и список найденных расхождений.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations_with_replacement

#: Сумма с валютой: «80 000 руб.», «9 122,19 рубля», «6470 ₽».
#: Разряды разделяются обычным или неразрывным пробелом.
_MONEY_RE = re.compile(
    r"(?<![\d/.,])"
    r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)"
    r"(?:[.,](\d{1,2}))?"
    r"\s*(?:руб(?:\.|лей|ля|ль)?|₽|р\.)",
    re.IGNORECASE,
)

#: Любое число в исходниках: нужно, чтобы понять, встречается ли сумма
#: вообще. Годы, номера статей и пунктов сюда тоже попадают — это
#: допустимо: нас интересует только «нет такого числа нигде».
#: Разряды, разделённые пробелом («80 000»), обязаны читаться как одно
#: число, иначе 80 000 распадается на «80» и «000» и подтверждённая
#: сумма помечается выдуманной.
_NUMBER_RE = re.compile(r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


@dataclass(frozen=True)
class UnsourcedAmount:
    """Сумма, которой нет в материалах дела."""

    raw: str
    value: float


def _to_float(int_part: str, frac: str | None) -> float:
    digits = re.sub(r"[ \u00a0]", "", int_part)
    if frac:
        return float(f"{digits}.{frac}")
    return float(digits)


def _format(raw: str, frac: str | None) -> str:
    return f"{raw},{frac}" if frac else raw


def extract_money(text: str) -> list[tuple[str, float]]:
    """Найти суммы в тексте. Возвращает (как написано, значение)."""
    found: list[tuple[str, float]] = []
    for match in _MONEY_RE.finditer(text or ""):
        int_part, frac = match.group(1), match.group(2)
        try:
            value = _to_float(int_part, frac)
        except ValueError:  # pragma: no cover - защита от неожиданного формата
            continue
        found.append((_format(int_part.strip(), frac), value))
    return found


def _source_numbers(texts: Iterable[str], amounts: Iterable[str]) -> set[float]:
    """Все числа из исходников, приведённые к float."""
    values: set[float] = set()

    def add(piece: str) -> None:
        cleaned = re.sub(r"[ \u00a0]", "", piece).replace(",", ".")
        try:
            values.add(float(cleaned))
        except ValueError:
            return

    for text in texts:
        for raw in _NUMBER_RE.findall(text or ""):
            add(raw)
    for raw in amounts:
        # pack.amounts приходит строками вида «80 000» или «9 122,19»
        for piece in _NUMBER_RE.findall(raw or ""):
            add(piece)
    return values


def _is_sum_of_known(value: float, known: set[float]) -> bool:
    """Итог из известных слагаемых тоже считается подтверждённым.

    Резолютивная часть и мотивировочная часто приводят «всего
    подлежит взысканию», которого в деле нет по определению: это
    арифметика по подтверждённым суммам, а не выдумка.

    Слагаемые могут повторяться и их бывает больше трёх: на реальных
    актах встречается «82 500 = 27 500 × 3» и «105 000 = 50 000 +
    10 000 + 10 000 + 35 000». Ограничение двумя-тремя разными
    значениями помечало такие итоги как выдуманные (5 ложных
    срабатываний на 20 эталонных решений).
    """
    pool = sorted(
        {v for v in known if 0 < v <= value + 0.01}, reverse=True
    )[:12]
    if not pool:
        return False
    for size in (2, 3, 4):
        for combo in combinations_with_replacement(pool, size):
            if abs(sum(combo) - value) <= 0.05:
                return True
    return False


_TOTAL_MARKERS = ("всего", "итого", "в общей сумме", "общая сумма", "всего подлежит")

#: Производные величины: суд не берёт их из материалов, а вычисляет
#: (штраф — доля от взысканного, платёж — итог за период, неустойка —
#: процент). Такая сумма объяснима, если она — простая доля или
#: кратное известного числа.
_DERIVED_MARKERS = (
    "штраф", "ежемесячн", "моральн", "платеж", "платёж", "неустойк",
    "пеня", "пени",
)

#: Допустимые простые соотношения: половина, треть, двойная, тройная.
_DERIVED_RATIOS = ((0.5, 1 / 3), (2.0, 3.0))


def _near_marker(text: str, start: int, markers: tuple[str, ...]) -> bool:
    window = (text[max(0, start - 120):start] or "").lower()
    return any(marker in window for marker in markers)


def _is_simple_derivative(value: float, pool: set[float]) -> bool:
    """Является ли сумма простой долей или кратным известного числа."""
    for base in pool:
        if base <= 0:
            continue
        for fraction in _DERIVED_RATIOS[0]:
            if abs(base * fraction - value) <= 0.05:
                return True
        for factor in _DERIVED_RATIOS[1]:
            if abs(base * factor - value) <= 0.05:
                return True
    return False


def _near_total(text: str, start: int) -> bool:
    """Стоит ли сумма рядом со словом «всего/итого»."""
    window = (text[max(0, start - 120):start] or "").lower()
    return any(marker in window for marker in _TOTAL_MARKERS)


def check_unsourced_amounts(
    text: str,
    source_documents: Iterable[str],
    known_amounts: Iterable[str] = (),
) -> list[UnsourcedAmount]:
    """Суммы из текста, которых нет ни в одном документе дела."""
    known = _source_numbers(source_documents, known_amounts)
    # Числа самого акта: итог часто считается из его же слагаемых,
    # которых в материалах нет («27 500 × 3 = 82 500»).
    local: set[float] = set()
    for piece in _NUMBER_RE.findall(text or ""):
        try:
            local.add(float(piece.replace(" ", "").replace(",", ".")))
        except ValueError:
            continue

    result: list[UnsourcedAmount] = []
    seen: set[float] = set()

    for match in _MONEY_RE.finditer(text or ""):
        int_part, frac = match.group(1), match.group(2)
        try:
            value = _to_float(int_part, frac)
        except ValueError:  # pragma: no cover
            continue
        raw = _format(int_part.strip(), frac)
        if value in seen:
            continue
        seen.add(value)
        if value in known:
            continue
        if _is_sum_of_known(value, known):
            continue
        # Итог, посчитанный из чисел самого акта, — арифметика, а не
        # выдумка. Проверяется только рядом со словом «всего/итого»:
        # без этой привязки любая сумма могла бы «объясниться» чужой.
        if _near_total(text, match.start()) and _is_sum_of_known(
            value, (known | local) - {value}
        ):
            continue
        # Производная величина: штраф, платёж за период, неустойка.
        if _near_marker(text, match.start(), _DERIVED_MARKERS) and _is_simple_derivative(
            value, (known | local) - {value}
        ):
            continue
        result.append(UnsourcedAmount(raw=raw, value=value))
    return result


def mark_unsourced_amounts(
    text: str,
    source_documents: Iterable[str],
    known_amounts: Iterable[str] = (),
    marker: str = "[сумма отсутствует в материалах дела — проверьте]",
) -> tuple[str, list[UnsourcedAmount]]:
    """Пометить в тексте суммы, которых нет в материалах дела.

    Удалять сумму нельзя: она может быть верной, но взятой из
    документа, который не загрузили. Задача — не пропустить её в акт
    молча, поэтому ставится пометка прямо после числа.
    """
    unsourced = check_unsourced_amounts(text, source_documents, known_amounts)
    if not unsourced:
        return text, []

    bad = {item.raw for item in unsourced}
    marked = text
    for raw in sorted(bad, key=len, reverse=True):
        pattern = re.compile(re.escape(raw) + r"\s*(?:руб(?:\.|лей|ля|ль)?|₽|р\.)", re.IGNORECASE)
        marked = pattern.sub(lambda m: f"{m.group(0)} {marker}", marked, count=1)
    return marked, unsourced
