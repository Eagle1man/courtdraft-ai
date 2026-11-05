"""Детерминированное извлечение сумм (этап 2, §8.1).

Модель плохо находит суммы в реальных исках: число записано как
«1 256 850 (Один миллион двести пятьдесят шесть тысяч восемьсот
пятьдесят) рублей 00 копеек», и 1.5B возвращает пустой список. Это не
повод «улучшать промпт» бесконечно — число находит Python, а модель
нужна там, где требуется понимание.

Модуль возвращает суммы с видом и цитатой; вид определяется по словам
рядом с числом. Ошибка классификации видна в отчёте и не скрывается.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.extraction.schemas import CaseAmount, SourceRef, parse_money

#: Число с пробелами/неразрывными пробелами, необязательными копейками и
#: словом «руб» рядом. Границы исключают годы и номера домов.
_AMOUNT = re.compile(
    r"(?<![\d.])(\d{1,3}(?:[ \u00a0]\d{3})+|\d{4,})(?:[.,](\d{1,2}))?"
    r"(?=[^.\d]{0,120}?(?:руб|₽|р\.))",
    re.IGNORECASE,
)

#: Ключевые слова вида суммы. Порядок важен: «цена иска» точнее, чем «долг».
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("costs", ("пошлин",)),
    ("penalty", ("неустойк", "пени", "штраф")),
    ("interest", ("процент",)),
    ("claim", ("цена иска",)),
    ("total", ("общая сумма", "итого", "всего взыскать")),
    ("paid", ("оплат", "платеж", "платёж", "перечислил")),
    ("principal", ("задолженност", "основной долг", "остаток долга", "долг")),
)

#: Сколько знаков контекста смотреть слева и справа.
_WINDOW_LEFT = 140
_WINDOW_RIGHT = 60


def classify_kind(context: str) -> str:
    """Вид суммы по словам вокруг числа.

    Берётся ближайшее ключевое слово, а не первое найденное: в строке
    «Размер неустойки: 1 225 000 × 0,1% × 26 = 31 850» слово «неустойка»
    стоит рядом с первым числом, но это расчёт, а не сама неустойка.
    """
    text = context.lower()
    best_kind = "other"
    best_distance = len(text) + 1
    for kind, words in _KINDS:
        for word in words:
            position = text.find(word)
            while position >= 0:
                if position < best_distance:
                    best_distance = position
                    best_kind = kind
                position = text.find(word, position + 1)
    return best_kind


def classify_near(left: str, right: str) -> tuple[str, int]:
    """Вид суммы и расстояние до ключевого слова (чем меньше, тем точнее)."""
    best_kind = "other"
    best_distance = 10**6
    left_text = left.lower()
    right_text = right.lower()
    for kind, words in _KINDS:
        for word in words:
            position = left_text.rfind(word)
            if position >= 0:
                distance = len(left_text) - position
                if distance < best_distance:
                    best_distance = distance
                    best_kind = kind
            position = right_text.find(word)
            if position >= 0:
                # Ключевое слово после числа встречается и в следующем
                # предложении («… 31 850 рублей. Общая сумма …»), поэтому
                # правое совпадение считается менее надёжным.
                distance = position + 1 + 20
                if distance < best_distance:
                    best_distance = distance
                    best_kind = kind
    return best_kind, best_distance


#: Знаки арифметики: число рядом с ними — часть расчёта, а не итог.
_ARITHMETIC = ("×", "x", "*", "=")


def _is_formula(text: str, end: int) -> bool:
    tail = text[end : end + 4]
    return any(symbol in tail for symbol in _ARITHMETIC)


#: Приоритет вида: при повторе одной суммы выбирается более конкретный.
#: «Задолженность 1 225 000» точнее, чем «оплата 1 225 000».
_KIND_PRIORITY = [
    "costs",
    "penalty",
    "interest",
    "claim",
    "total",
    "principal",
    "paid",
    "other",
]


def _priority(kind: str) -> int:
    return _KIND_PRIORITY.index(kind) if kind in _KIND_PRIORITY else len(_KIND_PRIORITY)


def extract_amounts(
    text: str,
    *,
    document_id: str = "",
    with_source: bool = True,
) -> list[CaseAmount]:
    """Найти суммы в тексте и определить их вид.

    Одно и то же число встречается в иске несколько раз (расчёт, текст
    требования, резолютивная часть). Хранится одна запись с наиболее
    конкретным видом: пять копий — шум, а не пять фактов.
    """
    if not text:
        return []

    by_value: dict[str, tuple[CaseAmount, int]] = {}

    for match in _AMOUNT.finditer(text):
        if _is_formula(text, match.end()):
            continue

        integer = match.group(1).replace("\u00a0", " ").strip()
        fraction = match.group(2) or ""
        value_text = integer + (f",{fraction}" if fraction else "")
        value = parse_money(value_text)
        if value is None or value <= 0:
            continue

        left = max(0, match.start() - _WINDOW_LEFT)
        right = min(len(text), match.end() + _WINDOW_RIGHT)
        kind, distance = classify_near(text[left : match.start()], text[match.end() : right])

        source = None
        if with_source:
            # Цитата — окружение числа, но обязательно с «руб»: иначе
            # проверка источника не поймёт, что это денежная запись.
            quote_left = max(0, match.start() - 60)
            quote_right = min(len(text), match.end() + 90)
            quote = " ".join(text[quote_left:quote_right].split())[:300]
            source = SourceRef(document_id=document_id, quote=quote)

        key = str(value)
        existing = by_value.get(key)
        # При повторе одной суммы выигрывает та запись, где ключевое слово
        # ближе к числу: «задолженность составляет 1 225 000» точнее, чем
        # «оплату в размере 1 225 000» в перечислении событий.
        if existing is None or distance < existing[1]:
            by_value[key] = (CaseAmount(kind=kind, value=value, source=source), distance)

    found = [item for item, _distance in by_value.values()]
    # Крупные суммы важнее мелочи: в отчёт и в реестр они попадают первыми.
    found.sort(key=lambda item: item.value, reverse=True)
    return found


def total_of(amounts: list[CaseAmount]) -> Decimal | None:
    """Сумма «итого», если она найдена явно."""
    for item in amounts:
        if item.kind == "total":
            return item.value
    return None
