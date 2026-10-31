"""Схемы API для дел и документов (п.9, п.10, п.42).

Валидация Zod-подобная через Pydantic: недопустимые значения отсекаются
на границе, а не внутри бизнес-логики.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.entities import CaseStatus, DocumentType


class CaseCreate(BaseModel):
    """Создание дела."""

    case_number: str = Field(min_length=1, max_length=100, examples=["DEMO-001"])
    court_name: str = Field(min_length=1, max_length=300)
    case_type: str = Field(min_length=1, max_length=100, examples=["civil"])
    category: str = Field(default="", max_length=100)
    # Обязательное поле для демо-данных (п.40): синтетическое дело должно
    # быть помечено, чтобы его нельзя было спутать с реальным.
    is_synthetic: bool = Field(
        default=False,
        description="Помечает данные как синтетические. Для демо обязательно true.",
    )

    model_config = ConfigDict(extra="forbid")


class CaseUpdate(BaseModel):
    """Изменение дела."""

    category: str | None = Field(default=None, max_length=100)
    status: CaseStatus | None = None

    model_config = ConfigDict(extra="forbid")


class ParticipantOut(BaseModel):
    """Участник дела (п.34)."""

    id: uuid.UUID
    name: str
    role: str
    is_anonymized: bool


class DocumentOut(BaseModel):
    """Документ дела (п.10)."""

    id: uuid.UUID
    case_id: uuid.UUID
    filename: str
    document_type: str
    mime_type: str
    page_count: int
    sha256: str
    # Признак, что тип не входит в базовый перечень (п.11: расширяемость).
    is_known_type: bool = True
    # Длина текста и наличие меток обезличивания («<ДАТА23>»): интерфейс
    # показывает и то и другое в списке, не выгружая весь текст документа.
    characters: int = 0
    has_placeholders: bool = False
    #: native — текстовый слой файла, ocr — распознано с изображения.
    #: Текст OCR может содержать ошибки распознавания, и это видно сразу.
    text_source: str = "native"
    #: В тексте найдены фразы, похожие на инструкции модели.
    injection_warning: bool = False
    #: Предупреждения текущей загрузки (дубликат, OCR, инструкции).
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentDetailOut(DocumentOut):
    """Документ с полным текстом — для просмотра материала дела."""

    text: str = ""
    # Уникальные метки вида <ДАТА23> — подсвечиваются в просмотрщике.
    placeholders: list[str] = Field(default_factory=list)


class CaseOut(BaseModel):
    """Дело (п.9, п.34)."""

    id: uuid.UUID
    case_number: str
    court_name: str
    case_type: str
    category: str
    status: str
    is_synthetic: bool
    created_at: datetime
    updated_at: datetime
    participants: list[ParticipantOut] = Field(default_factory=list)
    documents: list[DocumentOut] = Field(default_factory=list)
    # Счётчики для списка дел — без отдельных запросов на каждый.
    document_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class CaseListOut(BaseModel):
    """Список дел."""

    items: list[CaseOut]
    total: int


class DocumentRegister(BaseModel):
    """Регистрация документа (Фаза 5 загрузит файл).

    Сейчас принимаются метаданные и текст: загрузка бинарных файлов
    придёт в Фазе 5 вместе с извлечением текста.
    """

    filename: str = Field(min_length=1, max_length=500)
    document_type: str = Field(default=DocumentType.OTHER.value, max_length=50)
    mime_type: str = Field(default="text/plain", max_length=150)
    page_count: int = Field(default=0, ge=0, le=10000)
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
