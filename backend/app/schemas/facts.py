"""Схемы API реестра фактов (этапы 2–3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FactsJobOut(BaseModel):
    """Ответ на запуск извлечения: задание, а не результат.

    Извлечение идёт через модель и занимает минуты, поэтому HTTP-ответ
    не ждёт его: клиент получает job_id и опрашивает состояние.
    """

    job_id: uuid.UUID
    case_id: uuid.UUID
    status: str
    started_at: datetime


class CaseFactsOut(BaseModel):
    """Снимок реестра фактов дела."""

    id: uuid.UUID
    case_id: uuid.UUID
    status: str = Field(description="ok | needs_review")
    model: str
    documents: int
    duration_sec: float
    summary: dict[str, Any] = Field(default_factory=dict)
    registry: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
