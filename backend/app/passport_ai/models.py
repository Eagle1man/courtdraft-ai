"""Внутренние модели паспорт-модуля. Не являются схемой паспорта."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class FactStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    DERIVED = "DERIVED"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNSUPPORTED = "UNSUPPORTED"


class Stage(StrEnum):
    NEW = "new"
    INTAKE = "intake"
    READ = "read"
    CLASSIFY = "classify"
    FACTS = "facts"
    RECONCILE = "reconcile"
    DRAFT = "draft"
    VALIDATED = "validated"
    CONFIRMED = "confirmed"


class Locator(BaseModel):
    document_id: str
    filename: str = ""
    page: int | None = None
    block: int | None = None
    table: str | None = None
    row: int | None = None
    start: int | None = None
    end: int | None = None


class Fragment(BaseModel):
    locator: Locator
    text: str
    kind: str = "text"  # text | heading | table_row


class DocIntake(BaseModel):
    document_id: str
    filename: str
    size: int
    sha256: str
    format: str = "unknown"
    pages: int | None = None
    duplicate_of: str | None = None
    read_ok: bool | None = None
    errors: list[str] = Field(default_factory=list)


class DocClass(BaseModel):
    doc_type: str
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    by_llm: bool = False


class FactCandidate(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    role: str
    value_raw: str
    value_norm: Any = None
    quote: str
    locator: Locator
    party: str = ""
    doc_type: str = ""
    status: FactStatus = FactStatus.SUPPORTED
    confidence: float = 0.0
    model: str = ""
    note: str = ""
    source_fact_ids: list[str] = Field(default_factory=list)


class Conflict(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    role: str
    kind: str  # amount | date | name | requisite | other
    variants: list[dict[str, Any]] = Field(default_factory=list)
    blocking: bool = False
    note: str = ""


class FieldDraft(BaseModel):
    role: str
    pointer: str | list[str]
    value: Any = None
    status: FactStatus = FactStatus.NOT_FOUND
    fact_ids: list[str] = Field(default_factory=list)
    conflict_id: str | None = None
    required: bool = False
    kind: str = "text"
    user_action: str | None = None  # confirmed | edited | rejected


class ValidationFinding(BaseModel):
    level: str  # error | warning
    code: str
    message: str
    field: str = ""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
