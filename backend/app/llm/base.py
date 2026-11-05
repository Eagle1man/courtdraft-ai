"""Интерфейс LLM-провайдера.

Ключевое требование ТЗ (п.5, п.69): бизнес-логика не знает о конкретной модели.
Агенты работают только через LLMProvider, поэтому переключение MOCK -> REAL
не требует правок в агентах.

Реализации: MockLLMProvider (llm/mock.py), OllamaProvider (llm/ollama_provider.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class LLMRole(StrEnum):
    """Роли вызова. Позволяют провайдеру подбирать оптимальные параметры.

    Судебная подготовка требует низкой креативности: нужна точная работа с
    фактами из источника, а не свободное письмо.
    """

    EXTRACTION = "extraction"      # извлечение фактов, участников, дат
    CLASSIFICATION = "classification"  # определение типа документа
    REASONING = "reasoning"        # анализ, выводы, сопоставление
    GENERATION = "generation"      # формирование текста проекта


class LLMMessage(BaseModel):
    """Сообщение в формате, общем для всех провайдеров."""

    role: str = Field(description="system | user | assistant")
    content: str


class LLMRequest(BaseModel):
    """Запрос к модели. Не зависит от провайдера."""

    messages: Sequence[LLMMessage]
    role: LLMRole = LLMRole.REASONING
    temperature: float | None = None
    max_tokens: int | None = None
    json_mode: bool = Field(
        default=False,
        description="Ожидается валидный JSON в ответе",
    )
    json_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON Schema ответа (Ollama Structured Output). Если задана, "
            "модель обязана вернуть объект этой формы; json_mode тогда "
            "включается автоматически."
        ),
    )
    reasoning: bool | None = Field(
        default=None,
        description=(
            "Разрешено ли модели размышлять перед ответом. None — на "
            "усмотрение провайдера. False — ответ без рассуждений."
        ),
    )
    stop: Sequence[str] | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Служебное (например, id узла графа). На ответ не влияет.",
    )


class LLMUsage(BaseModel):
    """Расход токенов. Для MOCK считается приблизительно."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMResponse(BaseModel):
    """Ответ модели."""

    text: str
    usage: LLMUsage = Field(default_factory=LLMUsage)
    model: str = "unknown"
    provider: str = "unknown"
    # MOCK обязан себя помечать: иначе нельзя отличить реальный
    # вывод от заглушки в отчёте проверки.
    is_mock: bool = True
    latency_ms: int = 0
    finish_reason: str = "stop"


class LLMError(RuntimeError):
    """Ошибка LLM-провайдера.

    Отдельный тип, чтобы агенты могли отличать «модель не ответила»
    от «модель ответила ерундой». Первое — повод пометить запись
    как требующую проверки, а не подставлять вымысел.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Протокол LLM-провайдера.

    Реализуется MockLLMProvider и OllamaProvider. Проверяется в тестах,
    что обе реализации удовлетворяют одному протоколу.
    """

    name: str

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Сгенерировать ответ."""
        ...

    async def generate_json(
        self,
        request: LLMRequest,
        *,
        default: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Получить структурированный JSON-ответ.

        При неудаче возвращает default (или пустой словарь), а не бросает:
        агентам нужна устойчивость к невалидному ответу модели.
        """
        ...

    async def health(self) -> bool:
        """Проверить доступность. Не должно бросать исключений."""
        ...
