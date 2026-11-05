"""Схемы извлечения фактов (этап 2 ТЗ, §6.4).

Две группы моделей — это не дублирование, а граница ответственности:

* `ExtractionPayload` и `Raw*` — то, что возвращает модель. Поля простые
  (строки), потому что маленькая локальная модель плохо заполняет
  вложенные схемы с Decimal и типизированными датами.
* `CaseRegistry`, `CaseFact`, `CaseAmount`, `CaseDate` — то, с чем
  работает остальная система. Здесь Decimal, даты, статусы и источники.

Преобразование делает Python (`fact_extractor.build_registry`), а не
модель: если сумму или дату разобрать не удалось, это фиксируется как
пропуск с причиной, а не подменяется правдоподобным значением.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: Версия схемы. Хранится в реестре, чтобы старые записи можно было
#: прочитать после изменения модели.
SCHEMA_VERSION = "1.0"


class FactStatus(StrEnum):
    """Статус факта (ТЗ §6.4)."""

    EXTRACTED = "extracted"
    SOURCE_MATCHED = "source_matched"
    VERIFIED = "verified"
    CONFLICTED = "conflicted"
    UNSUPPORTED = "unsupported"
    NEEDS_REVIEW = "needs_review"


class ExtractionStatus(StrEnum):
    """Итог извлечения по документу или делу."""

    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class SourceRef(BaseModel):
    """Ссылка на место в документе (ТЗ §7.1)."""

    document_id: str
    page: int | None = None
    paragraph: int | None = None
    quote: str = ""

    @field_validator("quote")
    @classmethod
    def _trim(cls, value: str) -> str:
        return " ".join(value.split())[:500]


# ── То, что возвращает модель ────────────────────────────────────────────


class RawParticipant(BaseModel):
    role: str = ""
    name: str = ""
    inn: str = ""
    quote: str = ""


class RawAmount(BaseModel):
    kind: str = ""
    value: str = ""
    quote: str = ""


class RawDate(BaseModel):
    kind: str = ""
    value: str = ""
    quote: str = ""


class RawClaim(BaseModel):
    subject: str = ""
    amount: str = ""
    quote: str = ""


class RawFact(BaseModel):
    text: str = ""
    category: str = ""
    quote: str = ""


class ExtractionPayload(BaseModel):
    """Ответ модели. Только простые типы — их она заполняет надёжнее."""

    case_number: str = ""
    participants: list[RawParticipant] = Field(default_factory=list)
    amounts: list[RawAmount] = Field(default_factory=list)
    dates: list[RawDate] = Field(default_factory=list)
    claims: list[RawClaim] = Field(default_factory=list)
    facts: list[RawFact] = Field(default_factory=list)


# ── То, с чем работает система ───────────────────────────────────────────


class CaseParticipant(BaseModel):
    role: Literal[
        "plaintiff",
        "defendant",
        "third_party",
        "representative",
        "other",
    ] = "other"
    name: str
    inn: str | None = None
    source: SourceRef | None = None


class CaseAmount(BaseModel):
    kind: str
    value: Decimal = Field(ge=Decimal("0"))
    source: SourceRef | None = None


class CaseDate(BaseModel):
    kind: str
    value: date
    raw: str = ""
    source: SourceRef | None = None


class CaseClaim(BaseModel):
    subject: str
    amount: Decimal | None = Field(default=None, ge=Decimal("0"))
    source: SourceRef | None = None


class CaseFact(BaseModel):
    fact_id: str
    fact_type: str
    value: str
    source: SourceRef | None = None
    status: FactStatus = FactStatus.EXTRACTED
    confidence: float | None = Field(default=None, ge=0, le=1)


class Contradiction(BaseModel):
    kind: str
    values: list[str]
    sources: list[SourceRef] = Field(default_factory=list)
    blocking: bool = False


class MissingInformation(BaseModel):
    field: str
    reason: str
    blocking: bool = False


class CaseRegistry(BaseModel):
    """Единый реестр фактов дела — источник данных для генерации (§9)."""

    schema_version: str = SCHEMA_VERSION
    case_number: str | None = None
    participants: list[CaseParticipant] = Field(default_factory=list)
    amounts: list[CaseAmount] = Field(default_factory=list)
    dates: list[CaseDate] = Field(default_factory=list)
    claims: list[CaseClaim] = Field(default_factory=list)
    facts: list[CaseFact] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    missing: list[MissingInformation] = Field(default_factory=list)
    status: ExtractionStatus = ExtractionStatus.OK


# ── Разбор значений (Python, не модель) ──────────────────────────────────

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

_ROLE_MAP = {
    "истец": "plaintiff",
    "истца": "plaintiff",
    "ответчик": "defendant",
    "ответчика": "defendant",
    "третье лицо": "third_party",
    "третье": "third_party",
    "представитель": "representative",
    "plaintiff": "plaintiff",
    "defendant": "defendant",
    "third_party": "third_party",
    "representative": "representative",
}


def parse_money(raw: str) -> Decimal | None:
    """Строка с суммой в Decimal.

    Допускаются пробелы, «руб.», запятая как разделитель дробной части и
    минус (он означает не сумму, а ошибку разбора — возвращаем None).
    """
    if not raw:
        return None
    text = raw.replace("\u00a0", " ").strip()
    text = text.replace(" ", "").replace("руб.", "").replace("руб", "").replace("₽", "")
    text = text.replace(",", ".")
    if text in {"", "-", "—"}:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return value if value >= 0 else None


def parse_date(raw: str) -> date | None:
    """Дата из строки: 15.01.2025, 2025-01-15, «15 января 2025 г.».

    Неверная дата (31.02.2025) возвращает None: лучше зафиксировать
    пропуск, чем подставить соседний день.
    """
    if not raw:
        return None
    text = " ".join(raw.replace("\u00a0", " ").split()).lower()
    text = text.replace("г.", "").replace("года", "").strip(" .")

    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    parts = text.split()
    if len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit():
        day, month_word, year = parts
        for prefix, month in _MONTHS.items():
            if month_word.startswith(prefix):
                try:
                    return date(int(year), month, int(day))
                except ValueError:
                    return None
    return None


def normalize_role(raw: str) -> str:
    """Роль стороны из свободного текста модели."""
    text = (raw or "").strip().lower()
    for key, role in _ROLE_MAP.items():
        if key in text:
            return role
    return "other"
