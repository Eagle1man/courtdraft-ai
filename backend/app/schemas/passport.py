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
