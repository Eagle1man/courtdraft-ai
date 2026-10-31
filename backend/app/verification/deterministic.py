"""Детерминированные валидаторы (Этап 3 ТЗ).

Здесь только код: никаких обращений к моделям. Правило простое —
если проверку можно выполнить арифметикой, сравнением строк или
датами, она выполняется здесь, а не моделью.

Что проверяется:

* сумма и её **назначение** (долг, проценты, пошлина, расходы) —
  число может быть в деле, но относиться к другому требованию;
* даты: каждая дата проекта должна встречаться в материалах;
* итог: заявленная сумма «всего» должна равняться сумме слагаемых;
* обязательные проверки: невыполненная проверка — это не «чисто»,
  а техническая ошибка;
* технический сбой проверки.

Каждое срабатывание несёт `check_id`, который попадает в отчёт и в
датасет фикстур, поэтому идентификаторы стабильны.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

#: Сумма с валютой. Разряды — обычный или неразрывный пробел.
_MONEY_RE = re.compile(
    r"(?<![\d/.,])(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,](\d{1,2}))?"
    r"\s*(?:руб(?:\.|лей|ля|ль)?|₽|р\.)",
    re.IGNORECASE,
)

#: Дата в двух распространённых видах: 10.02.2024 и «10» февраля 2024.
_DATE_DOT_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_WORD_RE = re.compile(
    r"\b(\d{1,2})\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|август|"
    r"сентябр|октябр|ноябр|декабр)\w*\s+(\d{4})",
    re.IGNORECASE,
)

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "май": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}

#: Назначение суммы: как оно называется в акте и в материалах.
PURPOSES: dict[str, tuple[str, ...]] = {
    "долг": ("долг", "задолженност", "основной долг", "сумма займа"),
    "проценты": ("процент",),
    "пошлина": ("пошлин",),
    "расходы": ("расход", "представител"),
    "неустойка": ("неустойк", "пеня", "пени"),
}

#: Полоса вокруг суммы, в которой ищется назначение. Одна фраза, а не
#: весь раздел: иначе «долг» из соседнего предложения приклеится к
#: процентам.
_PURPOSE_WINDOW = 90

#: Границы фразы: точка, запятая, точка с запятой, союз «и»/«или».
#: Назначение ищется только внутри фразы. Без этого в
#: «долг 80 000 руб. и проценты 9 122,19 руб.» второе число получало
#: назначение «долг» из первой половины, а первое — «проценты» из
#: второй.
_CLAUSE_BREAK = re.compile(r"[.;,]\s+|\s+(?:и|или)\s+", re.IGNORECASE)


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Границы фразы вокруг суммы."""
    left_limit = 0
    for match in _CLAUSE_BREAK.finditer(text, 0, start):
        left_limit = match.end()
    right_limit = len(text)
    after = _CLAUSE_BREAK.search(text, end)
    if after is not None:
        right_limit = after.start()
    return max(left_limit, start - _PURPOSE_WINDOW), min(right_limit, end + _PURPOSE_WINDOW)


@dataclass(frozen=True)
class Finding:
    """Срабатывание детерминированной проверки."""

    check_id: str
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL
    reason: str
    fragment: str = ""


def _amount(match: re.Match[str]) -> Decimal | None:
    digits = match.group(1).replace("\u00a0", "").replace(" ", "")
    frac = match.group(2)
    raw = f"{digits}.{frac}" if frac else digits
    try:
        return Decimal(raw)
    except InvalidOperation:  # pragma: no cover - регэксп не даёт иного
        return None


def _purpose_near(text: str, start: int, end: int) -> str:
    """Назначение суммы по БЛИЖАЙШЕМУ ключевому слову внутри фразы.

    Фраза ограничивается `_clause_bounds`: назначение соседнего
    требования не должно приклеиваться к этой сумме.
    """
    left_limit, right_limit = _clause_bounds(text, start, end)
    left = text[left_limit:start].lower()
    right = text[end:right_limit].lower()

    best_name = ""
    best_distance = 10**9
    for name, needles in PURPOSES.items():
        for needle in needles:
            pos = left.rfind(needle)
            if pos >= 0:
                distance = len(left) - pos
                if distance < best_distance:
                    best_name, best_distance = name, distance
            pos = right.find(needle)
            if pos >= 0:
                distance = pos + 1
                if distance < best_distance:
                    best_name, best_distance = name, distance
    return best_name


def amounts_by_purpose(text: str) -> list[tuple[str, Decimal, str]]:
    """Суммы текста с назначением: (назначение, значение, как написано)."""
    found: list[tuple[str, Decimal, str]] = []
    for match in _MONEY_RE.finditer(text or ""):
        value = _amount(match)
        if value is None:
            continue
        purpose = _purpose_near(text, match.start(), match.end())
        if purpose:
            found.append((purpose, value, match.group(0).strip()))
    return found


def _source_pairs(sources: Iterable[str]) -> set[tuple[str, Decimal]]:
    pairs: set[tuple[str, Decimal]] = set()
    for text in sources:
        for purpose, value, _ in amounts_by_purpose(text):
            pairs.add((purpose, value))
    return pairs


def _known_values(sources: Iterable[str]) -> set[Decimal]:
    values: set[Decimal] = set()
    for text in sources:
        for match in _MONEY_RE.finditer(text or ""):
            value = _amount(match)
            if value is not None:
                values.add(value)
    return values


def check_amounts_by_purpose(text: str, sources: Iterable[str]) -> list[Finding]:
    """Сверить сумму вместе с её назначением.

    Два разных дефекта:

    * суммы нет в материалах вовсе;
    * сумма есть, но относится к другому требованию. Именно это
      пропускала проверка «число встречается в деле»: 80 000 руб. долга
      вместо 8 000 руб. платежа выглядели подтверждёнными.
    """
    source_list = list(sources)
    pairs = _source_pairs(source_list)
    values = _known_values(source_list)

    findings: list[Finding] = []
    seen: set[tuple[str, Decimal]] = set()
    for purpose, value, raw in amounts_by_purpose(text):
        if (purpose, value) in pairs or (purpose, value) in seen:
            continue
        seen.add((purpose, value))
        if value in values:
            findings.append(
                Finding(
                    check_id="AMOUNT_WRONG_PURPOSE",
                    severity="HIGH",
                    reason=(
                        f"сумма {raw} есть в материалах, но для другого "
                        f"назначения, а не для «{purpose}»"
                    ),
                    fragment=raw,
                )
            )
        else:
            findings.append(
                Finding(
                    check_id="AMOUNT_NOT_IN_SOURCES",
                    severity="CRITICAL",
                    reason=f"суммы {raw} нет в материалах дела",
                    fragment=raw,
                )
            )
    return findings


def _dates(text: str) -> list[tuple[int, int, int]]:
    """Даты текста в виде (день, месяц, год)."""
    out: list[tuple[int, int, int]] = []
    for match in _DATE_DOT_RE.finditer(text or ""):
        day, month, year = (int(g) for g in match.groups())
        out.append((day, month, year))
    for match in _DATE_WORD_RE.finditer(text or ""):
        day = int(match.group(1))
        stem = match.group(2).lower()
        month = next((m for key, m in _MONTHS.items() if stem.startswith(key)), 0)
        year = int(match.group(3))
        if month:
            out.append((day, month, year))
    return out


def check_dates(text: str, sources: Iterable[str]) -> list[Finding]:
    """Каждая дата проекта должна встречаться в материалах.

    Проверяется существование даты, а не её юридический смысл: дата
    события и дата документа различаются контекстом, и это уже не
    задача регулярного выражения.
    """
    known: set[tuple[int, int, int]] = set()
    for source in sources:
        known.update(_dates(source))

    findings: list[Finding] = []
    seen: set[tuple[int, int, int]] = set()
    for day, month, year in _dates(text):
        triple = (day, month, year)
        if triple in known or triple in seen:
            continue
        seen.add(triple)
        findings.append(
            Finding(
                check_id="DATE_NOT_IN_SOURCES",
                severity="HIGH",
                reason=(
                    f"дата {day:02d}.{month:02d}.{year} не встречается "
                    "в материалах дела"
                ),
                fragment=f"{day:02d}.{month:02d}.{year}",
            )
        )
    return findings


def check_total(text: str, parts: Iterable[Decimal], tolerance: Decimal = Decimal("0.01")) -> Finding | None:
    """Сверить заявленный итог с суммой слагаемых.

    Ищется сумма рядом со словом «всего»/«итого». Одно упоминание —
    один вывод: если итогов несколько, проверка молчит, потому что
    непонятно, к какому относятся слагаемые.
    """
    text = text or ""
    lowered = text.lower()
    total: Decimal | None = None
    for marker in ("всего", "итого", "в общей сумме", "всего подлежит"):
        index = lowered.find(marker)
        if index < 0:
            continue
        window = text[index:index + 200]
        matches = [m for m in _MONEY_RE.finditer(window)]
        if len(matches) != 1:
            return None
        total = _amount(matches[0])
        break

    if total is None:
        return None

    expected = sum((Decimal(p) for p in parts), start=Decimal("0"))
    if abs(total - expected) <= tolerance:
        return None
    return Finding(
        check_id="TOTAL_MISMATCH",
        severity="CRITICAL",
        reason=(
            f"итог {total} не равен сумме слагаемых {expected}"
        ),
        fragment=str(total),
    )


def missing_checks(executed: Iterable[str], required: Iterable[str]) -> list[Finding]:
    """Обязательные проверки, которые не выполнялись.

    Отсутствие проверки — не «дефектов нет», а незавершённая проверка.
    """
    done = set(executed)
    return [
        Finding(
            check_id="CHECK_NOT_EXECUTED",
            severity="HIGH",
            reason=f"обязательная проверка «{name}» не выполнялась",
            fragment=name,
        )
        for name in required
        if name not in done
    ]


def check_error(check_id: str, error: BaseException | str) -> Finding:
    """Технический сбой проверки: отдельный класс, не «замечание»."""
    detail = str(error)[:200]
    return Finding(
        check_id="CHECK_ERROR",
        severity="HIGH",
        reason=f"проверка «{check_id}» не выполнена: {detail}",
        fragment=check_id,
    )
