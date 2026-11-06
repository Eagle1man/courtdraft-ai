"""Схемы ответа /api/health (п.38).

Важно: ответ должен различать «компонент обязателен и не работает»
и «компонент опционален и не запущен». На ноутбуке разработки Ollama
и настоящие эмбеддинги не нужны, и их отсутствие — не поломка.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ComponentState(StrEnum):
    """Состояние компонента."""

    OK = "OK"                    # работает
    MOCK = "MOCK"                # работает в режиме заглушки — штатно на DEV
    NOT_REQUIRED = "NOT REQUIRED"  # не нужно в этом режиме
    UNAVAILABLE = "UNAVAILABLE"    # нужно и отсутствует — проблема
    ERROR = "ERROR"                # ошибка соединения


class ComponentHealth(BaseModel):
    """Состояние одного компонента."""

    name: str
    state: ComponentState
    detail: str = ""
    required: bool = True


class HealthResponse(BaseModel):
    """Ответ /api/health.

    Соответствует п.59: на ноутбуке LLM=MOCK, Embeddings=MOCK,
    GPU=NOT REQUIRED; на RTX 3050 — всё OK и GPU определён.
    """

    status: str = Field(description="OK | DEGRADED | ERROR")
    version: str
    environment: str = Field(description="development | runtime")
    components: list[ComponentHealth]
    # Человекочитаемый блок для scripts/health.ps1
    summary: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ApiInfoResponse(BaseModel):
    """Ответ /api/info — краткая идентификация сервиса."""

    name: str
    version: str
    environment: str
    llm_provider: str
    embedding_provider: str
    ollama_enabled: bool
    # Какие необязательные возможности включены на этом стенде. Клиент
    # по этому полю решает, показывать ли кнопку, а не по пробному
    # запросу «а вдруг сработает».
    features: dict[str, bool] = Field(default_factory=dict)
    # Явное предупреждение о статусе проекта (п.70).
    disclaimer: str
