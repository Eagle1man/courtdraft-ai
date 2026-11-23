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
    #: Статус раздела: ready / review / blocked. Вычисляется из needs_review,
    #: is_mock, наличия gaps и verification issues по этому разделу.
    status: str = "ready"


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
    #: Структурированный legal audit (Stage 3.x), если доступен.
    legal_audit: dict[str, Any] | None = None
    #: Per-claim proof bindings (claim↔span/manifest/page/ledger), additive.
    claim_proofs: list[dict[str, Any]] = Field(default_factory=list)
    claim_proof_summary: dict[str, Any] = Field(default_factory=dict)
    #: Число блокирующих замечаний (severity=CRITICAL).
    p0_count: int = 0
    #: Детали блокирующих замечаний для показа в интерфейсе.
    blocking: list[dict[str, str]] = Field(default_factory=list)


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
    document_kind: str = "judgment"
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

    #: Шаблон можно задать явно (демо, эксперименты). Пусто — выбирается
    #: по виду судопроизводства дела: арбитраж → АПК, иначе ГПК.
    template_name: str | None = Field(
        default=None,
        max_length=100,
        examples=[None, "civil_judgment", "arbitration_judgment", "civil_claim"],
    )
    #: Вид документа. Пусто — определяется по шаблону (civil_claim → claim).
    document_kind: str | None = Field(
        default=None,
        max_length=30,
        examples=[None, "judgment", "claim"],
    )
    #: Данные для конструктора иска (при document_kind=claim). Используются
    #: для детерминированных разделов и расчётов; модель этих данных не выдумывает.
    claim_data: dict[str, Any] | None = None
    # Кто инициировал: попадает в аудит и в автора версии.
    actor: str = Field(default="judge", max_length=60)

    model_config = ConfigDict(extra="allow")


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
    author_name: str = Field(default="", max_length=200)
    comment: str = Field(default="", max_length=2000)

    model_config = ConfigDict(extra="forbid")


class EditVariantOut(BaseModel):
    """Один из пяти вариантов формулировки для проблемного места."""

    index: int
    text: str
    #: Источник, из которого взят вариант. Есть не у всех — тогда пусто.
    source: str = ""
    #: Всегда needs_review: модель не имеет права ставить 'verified'.
    status: str = "needs_review"


class EditSpotOut(BaseModel):
    """Проблемное место с предложениями мини-агента."""

    spot_id: str
    section: str = ""
    reason: str
    original: str
    context: str = ""
    variants: list[EditVariantOut] = Field(default_factory=list)
    #: Всегда доступно: человек может не выбирать вариант, а написать свой.
    custom_allowed: bool = True


class EditSuggestionsOut(BaseModel):
    """Ответ мини-агента: список мест и по пять вариантов на каждое."""

    draft_id: uuid.UUID
    document_kind: str = "judgment"
    spots: list[EditSpotOut] = Field(default_factory=list)
    is_draft: bool = True
    disclaimer: str = DISCLAIMER


class DictateEditIn(BaseModel):
    """Голосовая команда «как секретарю»: что поправить в разделе."""

    command: str = Field(min_length=3, max_length=2000)
    section: str = Field(default="", max_length=200)
    author_name: str = Field(default="", max_length=200)


class DictateEditOut(BaseModel):
    """Предложение модели по голосовой команде (текст НЕ меняется)."""

    draft_id: uuid.UUID
    section: str = ""
    command: str = ""
    original: str = ""
    proposed: str = ""
    explanation: str = ""
    ok: bool = True
    disclaimer: str = DISCLAIMER


class GapOut(BaseModel):
    """Целевой вопрос по семантическому пробелу."""

    gap_id: str
    kind: str
    question: str
    reason: str
    options: list[str] = Field(default_factory=list)
    blocking: bool = False
    source_hint: str = ""
    answered: bool = False
    answer: str = ""


class GapAnswerIn(BaseModel):
    """Ответ человека: становится фактом с источником «ввод судьи»."""

    gap_id: str = Field(min_length=1, max_length=60)
    answer: str = Field(min_length=1, max_length=4000)
    author: str = Field(default="judge", pattern="^(secretary|judge)$")

    model_config = ConfigDict(extra="forbid")


class GapsOut(BaseModel):
    """Список вопросов по проекту."""

    draft_id: uuid.UUID
    gaps: list[GapOut] = Field(default_factory=list)
    blocked: bool = False
    is_draft: bool = True
    disclaimer: str = DISCLAIMER


class EditApplyIn(BaseModel):
    """Принятие одного варианта или собственной формулировки."""

    spot_id: str = Field(min_length=1, max_length=80)
    #: Индекс 0..4 из EditVariantOut (для справки/аудита); либо свой текст.
    variant_index: int | None = Field(default=None, ge=0, le=4)
    #: Текст выбранного варианта — именно он применяется, без повторной генерации.
    variant_text: str | None = Field(default=None, min_length=4, max_length=2000)
    custom_text: str | None = Field(default=None, min_length=4, max_length=2000)
    author: str = Field(default="judge", pattern="^(secretary|judge)$")
    author_name: str = Field(default="", max_length=200)
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

    #: Позиция в очереди (0 — первая/уже выполняется), оценка остатка в
    #: секундах и проценты готовности. Всё — оценка: модель не сообщает
    #: реальный прогресс, но очередь должна быть видна, а не «зависание».
    progress: int = 0
    stage: str = ""
    queue_position: int | None = None
    eta_seconds: int | None = None

    is_draft: bool = True
    disclaimer: str = DISCLAIMER
