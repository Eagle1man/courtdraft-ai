"""Фабрика LLM-провайдеров.

Единственное место в коде, где выбирается реализация LLMProvider.
Агенты вызывают get_llm_provider() и не знают, MOCK это или REAL (п.5, п.61).
"""

from __future__ import annotations

from functools import lru_cache

from app.config import LLMProviderName, Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.mock import MockLLMProvider
from app.llm.ollama_provider import OllamaProvider


def build_llm_provider(
    settings: Settings | None = None, *, model: str | None = None
) -> LLMProvider:
    """Создать провайдера по настройкам.

    `model` переопределяет OLLAMA_MODEL точечно: квота сильной модели
    решается для конкретного дела, а не для всего процесса.

    Если LLM_PROVIDER=ollama, но OLLAMA_ENABLED=false — это противоречие в
    конфигурации. Оно не должно приводить к падению: система обязана
    стартовать в degraded-режиме (п.53), поэтому возвращаем заглушку
    и оставляем след в логе.
    """
    cfg = settings or get_settings()

    if cfg.llm_provider is LLMProviderName.OLLAMA:
        if not cfg.ollama_enabled:
            return MockLLMProvider()

        return OllamaProvider(
            base_url=cfg.ollama_base_url,
            model=model or cfg.ollama_model,
            timeout_sec=cfg.ollama_timeout_sec,
            num_ctx=cfg.ollama_num_ctx,
            temperature=cfg.ollama_temperature,
        )

    return MockLLMProvider()


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    """Провайдер для приложения (создаётся один раз)."""
    return build_llm_provider()


def reset_llm_provider_cache() -> None:
    """Сбросить кэш. Нужен тестам, которые меняют настройки."""
    get_llm_provider.cache_clear()
