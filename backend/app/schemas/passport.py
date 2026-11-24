"""Схемы API паспорта дела и журнала расхождений."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PassportIssueOut(BaseModel):
    code: str
    severity: str
    reason: str


class PassportOut(BaseModel):
    case_id: uuid.UUID
    schema_id: str
    mode: str
    status: str
    source: str
    payload: dict[str, Any]
    issues: list[PassportIssueOut] = Field(default_factory=list)
    updated_at: datetime | None = None


class PassportIn(BaseModel):
    payload: dict[str, Any]
    source: str = "api"


class PassportValidationOut(BaseModel):
    status: str
    issues: list[PassportIssueOut] = Field(default_factory=list)


class DeviationOut(BaseModel):
    code: str
    severity: str
    payload: dict[str, Any]


class PassportAIStatusOut(BaseModel):
    stage: str
    updated_at: str | None = None
    fields_total: int = 0
    fields_filled: int = 0
    conflicts: int = 0
    blocking_issues: int = 0
    can_confirm: bool = False


class PassportFieldEditIn(BaseModel):
    value: Any = None
    action: str  # confirmed | edited | rejected


class PassportAIAnalysisOut(BaseModel):
    documents: list[dict[str, Any]] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    validation: list[dict[str, Any]] = Field(default_factory=list)
