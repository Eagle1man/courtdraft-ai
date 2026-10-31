"""Схемы API для проекта акта, версий и проверки (п.9, п.10, п.43).

Ответы проектируются так, чтобы интерфейс мог показать пользователю всё,
что нужно для решения о доверии тексту: сам текст, разбивку по разделам,
ссылки на источники и замечания проверки с указанием места в тексте.

Поле `is_draft` повторяется в каждом ответе намеренно: продукт нигде
не должен давать повод принять проект за решение суда.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.entities import DraftStatus

# Повторяется во всех ответах: это главное правило продукта, и оно должно
# быть видно в данных, а не только в интерфейсе.
DISCLAIMER = (
    "Это проект, а не решение суда. Проверка и принятие решения — за человеком."
)


class SectionOut(BaseModel):
    """Раздел проекта акта."""

    key: str
    name: str
    text: str
    # Отметка, что раздел получен от заглушки, а не от настоящей модели.
    is_mock: bool = False
    needs_review: bool = False
    citations: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    #: Чего не хватает в материалах дела. Показывается отдельным блоком,
    #: а не строкой в тексте: в акте не должно стоять
    #: «ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ».
    gaps: list[str] = Field(default_factory=list)


class IssueOut(BaseModel):
    """Замечание по проверке проекта."""

    type: str
    severity: str
    section: str = ""
    reason: str
    text: str = ""


class VerificationOut(BaseModel):
    """Результат проверки."""

    status: str
    issues: list[IssueOut] = Field(default_factory=list)
    claims_checked: int = 0
    unsupported_claims: int = 0
    sections_checked: int = 0
    #: Отчёт относится к другой версии документа: применять его нельзя,
    #: нужна повторная проверка (ТЗ §16).
    is_stale: bool = False
    #: Статус документа по правилам ТЗ §14: VERIFIED_WITHIN_SCOPE,
    #: REVIEW_REQUIRED, BLOCKED, VERIFICATION_FAILED.
    document_status: str = ""


class DraftVersionOut(BaseModel):
    """Версия проекта (п.31)."""

    id: uuid.UUID
    draft_id: uuid.UUID
    version: int
    text: str
    author: str
    comment: str = ""
    citations: list[Any] = Field(default_factory=list)
    created_at: datetime

    is_draft: bool = True
    disclaimer: str = DISCLAIMER


class DraftOut(BaseModel):
    """Проект акта с его последней версией и проверкой."""

    id: uuid.UUID
    case_id: uuid.UUID
    status: str
    template_name: str
    current_version: int
    created_at: datetime
    updated_at: datetime

    text: str = ""
    sections: list[SectionOut] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    verification: VerificationOut | None = None

    is_draft: bool = True
    disclaimer: str = DISCLAIMER


class DraftListOut(BaseModel):
    """Список версий проекта."""

    draft_id: uuid.UUID
    case_id: uuid.UUID
    status: str
    current_version: int
    versions: list[DraftVersionOut] = Field(default_factory=list)

    is_draft: bool = True
    disclaimer: str = DISCLAIMER


class DraftGenerateIn(BaseModel):
    """Запрос на формирование проекта."""

    template_name: str = Field(
        default="civil_judgment",
        max_length=100,
        examples=["civil_judgment", "world_judgment"],
    )
    # Кто инициировал: попадает в аудит и в автора версии.
    actor: str = Field(default="judge", max_length=60)

    model_config = ConfigDict(extra="forbid")


class DraftEditIn(BaseModel):
    """Правка проекта человеком.

    Текст присылается целиком, а не разметкой изменений: юридический акт
    нужно уметь показать в том виде, в каком он был принят.
    """

    text: str = Field(min_length=1, max_length=2_000_000)
    author: str = Field(
        description="Кто вносит правку: secretary или judge",
        pattern="^(secretary|judge)$",
        examples=["secretary"],
    )
    comment: str = Field(default="", max_length=2000)

    model_config = ConfigDict(extra="forbid")


class DraftStatusIn(BaseModel):
    """Смена статуса проекта."""

    status: DraftStatus

    model_config = ConfigDict(extra="forbid")


class EvidenceOut(BaseModel):
    """Факт пакета доказательств со ссылкой на источник."""

    id: uuid.UUID
    category: str
    content: str
    source: dict[str, Any] = Field(default_factory=dict)
    requires_review: bool = False

    @property
    def citation(self) -> str:
        """Человекочитаемая ссылка для показа в интерфейсе."""
        value = self.source.get("citation") or self.source.get("document") or ""
        return str(value)


class EvidencePackOut(BaseModel):
    """Пакет доказательств дела."""

    case_id: uuid.UUID
    facts: int
    gaps: list[str] = Field(default_factory=list)
    items: list[EvidenceOut] = Field(default_factory=list)


class DraftJobOut(BaseModel):
    """Фоновый запрос: принято в работу, результат позже.

    Формирование проекта занимает минуты, поэтому синхронный ответ
    невозможен: клиент получит таймаут задолго до готовности.
    """

    job_id: uuid.UUID
    case_id: uuid.UUID
    status: str
    started_at: datetime

    is_draft: bool = True
    disclaimer: str = DISCLAIMER
