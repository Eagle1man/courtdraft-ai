"""Проверка состояния компонентов (п.38, п.59).

Логика разделения на REQUIRED и NOT REQUIRED сосредоточена здесь:
на ноутбуке разработки отсутствие Ollama — норма, на машине с RTX 3050 —
неисправимость. Без этого разделения /api/health вводил бы в заблуждение.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import sys
from datetime import UTC, datetime

from app.config import AppEnv, QdrantMode, Settings, get_settings
from app.db.session import describe_url, is_sqlite
from app.embeddings import build_embedding_provider
from app.llm import build_llm_provider
from app.schemas.health import (
    ApiInfoResponse,
    ComponentHealth,
    ComponentState,
    HealthResponse,
)

logger = logging.getLogger(__name__)

VERSION = "0.1.0"

DISCLAIMER = (
    "ИИ помощник судьи формирует проект судебного акта на основании "
    "предоставленных "
    "материалов. Система не осуществляет правосудие и не принимает окончательных "
    "судебных решений. Проект подлежит обязательной проверке и редактированию "
    "уполномоченным специалистом. Возможны ошибки и неполнота исходных данных."
)


def _base_components(cfg: Settings) -> list[ComponentHealth]:
    """Компоненты, не требующие сети."""
    return [
        ComponentHealth(name="Backend", state=ComponentState.OK, detail=f"v{VERSION}"),
        ComponentHealth(
            name="Python",
            state=ComponentState.OK,
            detail=f"{platform.python_version()} ({sys.platform})",
        ),
        ComponentHealth(
            name="Storage",
            state=ComponentState.OK,
            detail=str(cfg.storage_dir),
            required=False,
        ),
    ]


async def _check_llm(cfg: Settings) -> ComponentHealth:
    """Состояние LLM. MOCK на DEV — это штатно, а не поломка."""
    # Строим провайдера из переданных настроек, а не берём глобальный
    # кэшированный: иначе проверка отражала бы прошлую конфигурацию.
    provider = build_llm_provider(cfg)

    if provider.name == "mock":
        return ComponentHealth(
            name="LLM",
            state=ComponentState.MOCK,
            detail="MockLLMProvider (реальная модель не вызывается)",
            required=False,
        )

    healthy = await provider.health()
    if healthy:
        detail = f"{provider.name} / {cfg.ollama_model}"
        if cfg.model_routing_enabled and cfg.strong_model_quota > 0:
            strong = cfg.strong_model or cfg.ollama_model
            weak = cfg.weak_model or cfg.ollama_model
            detail = (
                f"{provider.name} / {strong} — первые "
                f"{cfg.strong_model_quota} дел, затем {weak}"
            )
        return ComponentHealth(
            name="LLM",
            state=ComponentState.OK,
            detail=detail,
        )

    # В runtime-режиме реальная модель обязательна.
    return ComponentHealth(
        name="LLM",
        state=ComponentState.UNAVAILABLE if cfg.ollama_enabled else ComponentState.MOCK,
        detail=f"{cfg.ollama_model} недоступна на {cfg.ollama_base_url}",
        required=cfg.ollama_enabled,
    )


async def _check_ollama(cfg: Settings) -> ComponentHealth:
    """Состояние Ollama. На DEV она не требуется (п.38)."""
    if not cfg.ollama_enabled:
        return ComponentHealth(
            name="Ollama",
            state=ComponentState.NOT_REQUIRED,
            detail="не требуется в режиме разработки",
            required=False,
        )

    provider = build_llm_provider(cfg)
    healthy = provider.name != "ollama" or await provider.health()
    return ComponentHealth(
        name="Ollama",
        state=ComponentState.OK if healthy else ComponentState.UNAVAILABLE,
        detail=cfg.ollama_base_url if healthy else f"нет ответа от {cfg.ollama_base_url}",
        required=True,
    )


async def _check_embeddings(cfg: Settings) -> ComponentHealth:
    """Состояние эмбеддингов. Отсутствие модели не блокирует систему (п.53)."""
    provider = build_embedding_provider(cfg)

    if provider.name == "mock":
        return ComponentHealth(
            name="Embeddings",
            state=ComponentState.MOCK,
            detail="MockEmbeddingProvider",
            required=False,
        )

    healthy = await provider.health()
    if healthy:
        return ComponentHealth(
            name="Embeddings",
            state=ComponentState.OK,
            detail=f"{cfg.embedding_model}, dim={provider.dimension}",
        )

    state = ComponentState.NOT_REQUIRED if cfg.embeddings_optional else ComponentState.UNAVAILABLE
    return ComponentHealth(
        name="Embeddings",
        state=state,
        detail=f"{cfg.embedding_model} недоступна; degraded/mock mode",
        required=not cfg.embeddings_optional,
    )


async def _check_qdrant(cfg: Settings) -> ComponentHealth:
    """Состояние Qdrant.

    В режиме memory используется встроенный клиент qdrant-client:
    сервер поднимать не нужно, а поддержка payload-фильтров и поиска
    по сходству полная. Данные не переживают перезапуск — это осознанно
    для режима разработки, не для работы.

    Клиент берётся общий на процесс (`app.rag.shared`): файловый Qdrant
    не допускает второго открытия того же каталога, а health опрашивает
    каждый открытый браузер раз в минуту.
    """
    from app.rag.shared import get_store

    if cfg.qdrant_mode in {QdrantMode.MEMORY, QdrantMode.LOCAL}:
        local = cfg.qdrant_mode == QdrantMode.LOCAL
        try:
            store = get_store(cfg)
            alive = store.health()
            detail = (
                f"файловый индекс: {cfg.qdrant_path}"
                if local
                else "встроенный режим (memory), сервер не требуется; "
                "данные не переживают перезапуск"
            )
        except Exception as exc:  # noqa: BLE001
            state = ComponentState.UNAVAILABLE if local else ComponentState.MOCK
            return ComponentHealth(
                name="Qdrant",
                state=state,
                detail=f"хранилище недоступно: {str(exc)[:120]}",
                required=False,
            )

        return ComponentHealth(
            name="Qdrant",
            state=ComponentState.OK if alive else ComponentState.UNAVAILABLE,
            detail=detail,
            required=False,
        )

    # Серверный режим: недоступность здесь — уже проблема.
    try:
        store = get_store(cfg)
        alive = store.health()
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            name="Qdrant",
            state=ComponentState.UNAVAILABLE,
            detail=f"нет соединения с {cfg.qdrant_url}: {str(exc)[:100]}",
        )

    return ComponentHealth(
        name="Qdrant",
        state=ComponentState.OK if alive else ComponentState.UNAVAILABLE,
        detail=cfg.qdrant_url if alive else f"нет ответа от {cfg.qdrant_url}",
    )


async def _check_database(cfg: Settings) -> ComponentHealth:
    """Состояние БД.

    Проверяется реальным запросом через драйвер, а не TCP-портом:
    открытый порт не означает рабочую базу. Имя компонента отражает
    фактический диалект, чтобы не вводить в заблуждение.
    """
    from app.services.case_service import db

    if not db.available:
        return ComponentHealth(
            name="Database",
            state=ComponentState.UNAVAILABLE,
            detail="не инициализирована; проверьте DATABASE_URL в .env",
        )

    alive = await db.ping()
    if not alive:
        return ComponentHealth(
            name="Database",
            state=ComponentState.UNAVAILABLE,
            detail=f"нет ответа от БД ({db.url})",
        )

    engine_name = "SQLite (MOCK-режим)" if is_sqlite(db.url) else "PostgreSQL"
    return ComponentHealth(
        name="Database",
        state=ComponentState.OK,
        detail=f"{engine_name}: {describe_url(db.url)}",
    )


def _gpu_component(cfg: Settings) -> ComponentHealth:
    """GPU. Никогда не требуется на DEV (п.50)."""
    if cfg.app_env is AppEnv.DEVELOPMENT:
        return ComponentHealth(
            name="GPU",
            state=ComponentState.NOT_REQUIRED,
            detail="режим разработки, GPU не требуется",
            required=False,
        )
    return ComponentHealth(name="GPU", state=ComponentState.OK, detail="ожидается на RTX 3050")


async def build_health(settings: Settings | None = None) -> HealthResponse:
    """Собрать полный отчёт о состоянии.

    Настройки передаются явно: приложение может быть собрано с нестандартной
    конфигурацией (тесты, запуск с переопределениями), и проверка должна
    отражать именно её, а не глобальный .env.
    """
    cfg = settings or get_settings()

    components = list(_base_components(cfg))
    # Проверки независимы — выполняем параллельно, но с общим таймаутом.
    checks = await asyncio.gather(
        _check_database(cfg),
        _check_qdrant(cfg),
        _check_llm(cfg),
        _check_ollama(cfg),
        _check_embeddings(cfg),
        return_exceptions=True,
    )

    for check in checks:
        if isinstance(check, ComponentHealth):
            components.append(check)
        else:
            # Исключение при проверке — это ERROR, а не молчание.
            logger.warning("Проверка компонента не удалась: %s", check)
            components.append(
                ComponentHealth(
                    name="Unknown",
                    state=ComponentState.ERROR,
                    detail=str(check)[:200],
                )
            )

    components.append(_gpu_component(cfg))

    problems = [c for c in components if c.state is ComponentState.ERROR]
    degraded = [
        c for c in components if c.state is ComponentState.UNAVAILABLE and c.required
    ]

    if problems:
        status = "ERROR"
    elif degraded:
        status = "DEGRADED"
    else:
        status = "OK"

    warnings = [
        f"{c.name}: {c.detail}"
        for c in components
        if c.state in (ComponentState.UNAVAILABLE, ComponentState.MOCK)
    ]

    summary = {c.name: c.state.value for c in components}

    return HealthResponse(
        status=status,
        version=VERSION,
        environment=cfg.app_env.value,
        components=components,
        summary=summary,
        warnings=warnings,
    )


def build_info(cfg: Settings | None = None) -> ApiInfoResponse:
    """Краткая информация о сервисе."""
    settings = cfg or get_settings()
    return ApiInfoResponse(
        name=settings.app_name,
        version=VERSION,
        environment=settings.app_env.value,
        llm_provider=build_llm_provider(settings).name,
        embedding_provider=build_embedding_provider(settings).name,
        ollama_enabled=settings.ollama_enabled,
        disclaimer=DISCLAIMER,
    )


def server_started_at() -> datetime:
    """Метка времени запуска (для uptime в логах)."""
    return datetime.now(UTC)
