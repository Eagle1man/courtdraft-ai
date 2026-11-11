#!/usr/bin/env python3
"""Модель-агностичное ядро CourtDraft AI.

Единые парсеры сумм, норм и дат + профили моделей + журнал бюджета.
Не зависит от FastAPI и может использоваться скриптами оценки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_MONTH_INDEX = {name: index + 1 for index, name in enumerate(MONTHS)}

_AMOUNT_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[ \u00a0\u2009\u202f]\d{3})*(?:[.,]\d{1,2})?)"
    r"\s*(?:руб\w*\.?|₽)",
    re.IGNORECASE,
)
_LAW_RE = re.compile(
    r"(?:ст\.|стат(?:ья|ьи|ье|ьёй|ьей|ью|ьями|ьях|ьям)\.?)\s*"
    r"(\d+(?:\.\d+)*(?:\s*(?:,|и|–|—|-)\s*\d+(?:\.\d+)*)*)"
    r"\s*(?:([А-ЯЁ][А-ЯЁ ]{0,12}?)(?:РФ|кодекс))?",
    re.IGNORECASE,
)
_DATE_DOT_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_WORD_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b",
    re.IGNORECASE,
)


def amounts_kop(text: str) -> list[int]:
    """Все рублёвые суммы текста в копейках, без дублей подряд."""
    result: list[int] = []
    for match in _AMOUNT_RE.finditer(text or ""):
        raw = match.group(1).replace("\u00a0", "").replace("\u2009", "")
        raw = raw.replace("\u202f", "").replace(" ", "")
        if "," in raw:
            whole, frac = raw.split(",", 1)
        elif "." in raw:
            whole, frac = raw.split(".", 1)
        else:
            whole, frac = raw, "0"
        try:
            value = int(whole) * 100 + int((frac + "00")[:2])
        except ValueError:
            continue
        if value and (not result or result[-1] != value):
            result.append(value)
    return result


def citations(text: str) -> set[str]:
    """Нормативные ссылки в виде «333.19 ГК»."""
    found: set[str] = set()
    for match in _LAW_RE.finditer(text or ""):
        code = (match.group(2) or "").strip().upper()
        for article in re.split(r"\s*(?:,|и|–|—|-)\s*", match.group(1)):
            article = article.strip()
            if article:
                found.add(f"{article} {code}".strip())
    return found


def dates_any(text: str) -> list[date]:
    """Даты в форматах ДД.ММ.ГГГГ и «5 марта 2013»."""
    result: list[date] = []
    for day, month, year in _DATE_DOT_RE.findall(text or ""):
        try:
            result.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    for day, month, year in _DATE_WORD_RE.findall(text or ""):
        month_number = _MONTH_INDEX.get(month.lower())
        if month_number is None:
            continue
        try:
            result.append(date(int(year), month_number, int(day)))
        except ValueError:
            continue
    return result


@dataclass(frozen=True, slots=True)
class Profile:
    provider: str
    model: str
    ctx: int
    max_tokens: int
    retry_tokens: int
    context_chars: int
    model_sections: tuple[str, ...]


PROFILES: dict[str, Profile] = {
    "qwen2.5:1.5b": Profile(
        "ollama", "qwen2.5:1.5b", 4096, 420, 760, 2500, ("established",)
    ),
    "qwen2.5:7b": Profile(
        "ollama", "qwen2.5:7b", 8192, 700, 1100, 4000, ("established", "motivating")
    ),
    "qwen2.5:14b": Profile(
        "ollama", "qwen2.5:14b", 16384, 900, 1400, 6000, ("established", "motivating")
    ),
    "qwen2.5:32b": Profile(
        "ollama", "qwen2.5:32b", 32768, 1200, 1800, 8000, ("established", "motivating")
    ),
    "deepseek": Profile(
        "openai", "deepseek-flash", 128000, 1200, 1800, 12000, ("established", "motivating")
    ),
}

_DEFAULT = Profile("mock", "mock", 4096, 420, 760, 2500, ())


def profile_for(provider: str, model: str = "") -> Profile:
    """Профиль по провайдеру и имени модели."""
    provider = (provider or "").strip().lower()
    model = (model or "").strip().lower()
    if provider == "mock":
        return _DEFAULT
    if provider == "openai" or "deepseek" in model:
        return PROFILES["deepseek"]
    for key, profile in PROFILES.items():
        if key in model:
            return profile
    return Profile(
        provider=provider or "unknown",
        model=model or "unknown",
        ctx=8192,
        max_tokens=700,
        retry_tokens=1100,
        context_chars=4000,
        model_sections=("established", "motivating"),
    )


@dataclass
class Ledger:
    """Журнал фактического расхода API."""

    budget_rub: float
    spent_rub: float = 0.0
    entries: list[dict[str, Any]] = field(default_factory=list)

    def can_spend(self, estimate_rub: float) -> bool:
        return self.spent_rub + max(0.0, estimate_rub) <= self.budget_rub

    def add(self, cost_rub: float, **meta: Any) -> None:
        cost = max(0.0, float(cost_rub))
        self.spent_rub += cost
        self.entries.append({"cost_rub": cost, "at": datetime.now().isoformat(), **meta})

    @property
    def remaining_rub(self) -> float:
        return max(0.0, self.budget_rub - self.spent_rub)

    def summary(self) -> dict[str, Any]:
        return {
            "budget_rub": round(self.budget_rub, 6),
            "spent_rub": round(self.spent_rub, 6),
            "remaining_rub": round(self.remaining_rub, 6),
            "calls": len(self.entries),
        }
