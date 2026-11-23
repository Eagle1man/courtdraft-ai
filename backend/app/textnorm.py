"""Нормализация текста документа и детект разрывов чисел/слов."""
from __future__ import annotations

import re

# ruff: noqa: SIM905 — split() on multiline string is more readable than list literal

_INVISIBLE = tuple(chr(cp) for cp in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD, 0x2060))
_RE_MONEY_TOKEN = re.compile(r"(?<![\d.,])(\d[\d\u00a0 ]*),(\d{2})(?!\d)")
_RE_GROUPS = re.compile(r"\d{1,3}(?:[ \u00a0]\d{3})*")

# Частые слова юридического текста — для эвристики разрыва слов (P1, не блокирует).
_VOCAB = frozenset(
    "кодекса кодекс место городе платёжное платежное распиской расписка андрей "
    "автоматически подтверждается требует действовавшей состоялось заёмщик заемщик "
    "расчёту расчету процентов опись правовое ответа города нахождения почтового"
    .split()
)


def normalize(text: str) -> str:
    """Канонический вид: переводы строк, невидимые символы, хвостовые пробелы."""
    out = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for ch in _INVISIBLE:
        out = out.replace(ch, "")
    out = re.sub(r"\n{3,}", "\n\n", out)
    return "\n".join(line.rstrip() for line in out.split("\n")).strip()


def broken_numbers(text: str) -> list[str]:
    """Разрывы внутри денежных чисел: '1 0 263,29' — да, '90 000,00' — нет."""
    bad: list[str] = []
    for m in _RE_MONEY_TOKEN.finditer(text or ""):
        if not _RE_GROUPS.fullmatch(m.group(1)):
            bad.append(m.group(0))
    return bad


def fix_broken_numbers(text: str) -> str:
    """Перегруппировать разряды: '1 0 263,29' -> '10 263,29'."""
    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"[^\d]", "", m.group(1))
        grouped = f"{int(digits):,}".replace(",", " ")
        return f"{grouped},{m.group(2)}"
    return _RE_MONEY_TOKEN.sub(repl, text or "")


def broken_words(text: str) -> list[str]:
    """Эвристика разрыва слова: 'код екса' -> 'кодекса'. Сигнал P1, не блокирует."""
    found: list[str] = []
    for m in re.finditer(r"\b([а-яё]{3,})\s([а-яё]{1,3})\b", (text or "").lower()):
        joined = m.group(1) + m.group(2)
        if joined in _VOCAB:
            found.append(m.group(0))
    return found


def nbsp(text: str) -> str:
    """Неразрывные пробелы в числах и перед сокращениями — только для DOCX."""
    out = _RE_MONEY_TOKEN.sub(lambda m: m.group(0).replace(" ", "\u00a0"), text or "")
    out = re.sub(r"(?<=\d) (?=руб\.|л\.|коп\.)", "\u00a0", out)
    out = re.sub(r"\b([А-ЯЁ])\. (?=[А-ЯЁ]\.)", r"\1.\u00a0", out)
    return out
