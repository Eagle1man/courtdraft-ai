"""Схемы API для отзывов о проекте акта.

Отзыв — это не «лайк»: он собирается так, чтобы из него потом можно было
собрать датасет для дообучения. Поэтому обязательны вердикт и оценка,
а привязка к делу и версии проекта необязательна — отзыв без них тоже
полезен, но беднее.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.entities import FeedbackAuthorRole, FeedbackVerdict


class FeedbackIn(BaseModel):
    """Отзыв, который присылает человек."""

    rating: int = Field(ge=1, le=5, description="Оценка от 1 до 5")
    verdict: FeedbackVerdict = Field(
        default=FeedbackVerdict.NEEDS_WORK,
        description="Итог: проект можно брать в работу / нужна доработка / непригодно",
    )
    comment: str = Field(default="", max_length=5000)
    section_name: str = Field(default="", max_length=200)
    author_role: FeedbackAuthorRole = FeedbackAuthorRole.OTHER
    author_name: str = Field(default="", max_length=200)
    case_id: uuid.UUID | None = None
    draft_id: uuid.UUID | None = None
    is_anonymized: bool = True

    @field_validator("comment", "section_name", "author_name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class FeedbackOut(BaseModel):
    """Отзыв в ответе API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID | None = None
    draft_id: uuid.UUID | None = None
    draft_version: int = 0
    rating: int
    verdict: str
    section_name: str = ""
    comment: str = ""
    author_role: str
    author_name: str = ""
    model_name: str = ""
    is_anonymized: bool = True
    created_at: datetime


class FeedbackStatsOut(BaseModel):
    """Сводка по отзывам: средняя оценка и разбивки."""

    total: int = 0
    average_rating: float = 0.0
    by_verdict: dict[str, int] = Field(default_factory=dict)
    by_role: dict[str, int] = Field(default_factory=dict)


class FeedbackListOut(BaseModel):
    """Список отзывов вместе со сводкой по тому же фильтру."""

    items: list[FeedbackOut] = Field(default_factory=list)
    total: int = 0
    stats: FeedbackStatsOut = Field(default_factory=FeedbackStatsOut)
