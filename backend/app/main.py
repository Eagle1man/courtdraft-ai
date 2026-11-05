"""Точка входа FastAPI (Фаза 2).

Что уже работает:
  - приложение стартует без БД, без Qdrant и без модели эмбеддингов;
  - /api/health различает REQUIRED и NOT REQUIRED компоненты;
  - /api/docs открывается.

Модель и эмбеддинги не загружаются при старте — это принципиально для
машины 8 GB без GPU (п.50).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.routes import cases, demo, drafts, feedback, system
from app.config import Settings, get_settings
from app.db.bootstrap import MIGRATE_HINT, check_schema_drift, ensure_schema
from app.db.repositories import InterruptedJob, JobRepository
from app.db.session import describe_url
from app.services.case_service import db, session_scope
from app.services.health_service import DISCLAIMER, VERSION
from app.services.job_recovery import resume_interrupted

logging.basicConfig(
    level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("courtdraft")


class _NoCacheStaticFiles(StaticFiles):
    """Раздача интерфейса без кеширования в браузере.

    Без заголовка Cache-Control браузер применяет эвристическое
    кеширование и держит старую копию app.js: файл правится, сервер его
    отдаёт с новым ETag, а браузер показывает старое поведение. На
    демонстрации это выглядит как «правка не работает», и время уходит
    на проверку вчерашнего кода.

    На рабочей машине интерфейс открывают с localhost и правят часто, а
    размер файлов измеряется десятками килобайт — экономить трафик
    здесь нечего.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собрать приложение FastAPI."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Каталоги данных создаются при старте, а не вручную:
        # отсутствие каталога не должно быть причиной падения.
        cfg.ensure_dirs()

        # БД подключается мягко: её недоступность переводит систему в
        # degraded-режим, но не мешает подняться (подход п.53).
        # Настройки передаются явно: db.connect() иначе взял бы глобальный
        # .env, а приложение может быть собрано с другой конфигурацией.
        if await db.connect(cfg):
            await ensure_schema()
            logger.info("БД подключена: %s", describe_url(db.url))

            missing = await check_schema_drift()
            if missing:
                # Рассинхрон обнаружен здесь, а не в середине фоновой
                # задачи: сообщение в логе читается сразу, а не спустя
                # три минуты после нажатия кнопки.
                logger.error(
                    "Схема БД отстаёт от моделей: нет колонок %s\n%s",
                    ", ".join(missing),
                    MIGRATE_HINT,
                )

            # Очередь заданий жила в памяти: после остановки процесса
            # незавершённые задания не продолжались. Теперь их состояние в
            # базе, и незавершённые возвращаются в работу — кроме тех, что
            # уже прерывались: повторять вечно задание, которое валит
            # процесс, нельзя.
            recovered: list[InterruptedJob] = []
            try:
                async with session_scope(db.factory) as session:
                    repo = JobRepository(session)
                    if cfg.job_resume_enabled:
                        recovered = await repo.claim_interrupted(
                            max_attempts=cfg.job_resume_max_attempts
                        )
                    else:
                        interrupted = await repo.mark_interrupted()
                        if interrupted:
                            logger.warning(
                                "Прервано заданий после перезапуска: %d", interrupted
                            )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось разобрать прерванные задания: %s", exc)

            if recovered:
                # Отдельной задачей, а не в старте: генерация идёт минуты,
                # и ждать её до открытия порта нельзя.
                app.state.jobs_recovery = asyncio.create_task(
                    resume_interrupted(cfg, recovered), name="jobs-recovery"
                )
        else:
            logger.warning(
                "БД недоступна. Система работает без эндпоинтов дел. "
                "Проверьте DATABASE_URL в .env"
            )

        logger.info(
            "CourtDraft AI %s стартует: env=%s llm=%s embeddings=%s",
            VERSION,
            cfg.app_env.value,
            cfg.llm_provider.value,
            cfg.embedding_provider.value,
        )
        if not cfg.is_real_mode:
            logger.info("MOCK-режим: реальная LLM не вызывается (это штатно для разработки)")
        yield
        # Возобновлённые задания отменяются вместе со службой: иначе
        # фоновый цикл переживёт приложение и будет держать модель.
        recovery = getattr(app.state, "jobs_recovery", None)
        if recovery is not None and not recovery.done():
            recovery.cancel()
            with suppress(asyncio.CancelledError):
                await recovery
        # Файловый Qdrant держит каталог индекса: закрываем клиент явно,
        # иначе следующий запуск упрётся в блокировку прошлого процесса.
        from app.rag.shared import close_store

        close_store()
        await db.disconnect()
        logger.info("ИИ помощник судьи остановлен")

    app = FastAPI(
        title="ИИ помощник судьи",
        version=VERSION,
        description=(
            "Локальная подготовка проекта судебного акта.\n\n"
            f"⚠️ {DISCLAIMER}"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # Настройки кладём в состояние приложения: роутеры и health-check должны
    # видеть ту конфигурацию, с которой приложение собрано, а не глобальный .env.
    app.state.settings = cfg

    # Только локальные Origins (п.44): никакого облака.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system.router)
    app.include_router(cases.router)
    app.include_router(drafts.router)
    app.include_router(feedback.router)
    app.include_router(demo.router)

    @app.get("/api", include_in_schema=False)
    async def api_root() -> dict[str, str]:
        """Корень API: указать, где документацию и интерфейс."""
        info: dict[str, str] = {
            "name": "ИИ помощник судьи",
            "version": VERSION,
            "docs": "/docs",
            "health": "/api/health",
        }
        if cfg.frontend_available:
            info["ui"] = "/app"
        return info

    @app.get("/", include_in_schema=False)
    async def root() -> Any:
        """Корень: открыть интерфейс, а если его нет — показать адреса.

        Человек, запустивший сервис, идёт по адресу без всякого /app:
        заставлять его угадывать путь к интерфейсу незачем.
        """
        if cfg.frontend_available:
            return RedirectResponse(url="/app")
        return {
            "name": "ИИ помощник судьи",
            "version": VERSION,
            "docs": "/docs",
            "health": "/api/health",
            "note": "Веб-интерфейс не собран. API доступен по /api.",
        }

    if cfg.frontend_available:
        # Интерфейс раздаётся тем же приложением: один порт, один
        # источник происхождения, никакого CORS и отдельного сервера.
        app.mount(
            "/app",
            _NoCacheStaticFiles(
                directory=str(cfg.frontend_dir), html=True
            ),
            name="ui",
        )
    else:
        logger.warning(
            "Веб-интерфейс не найден в %s. API работает, интерфейс недоступен.",
            cfg.frontend_dir,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        """Единый формат необработанных ошибок.

        Текст исключения возвращается в ответе только в debug-режиме:
        в сообщениях об ошибках не должно быть содержимого судебных документов.
        """
        logger.exception("Необработанная ошибка при обработке %s", request.url.path)
        detail = str(exc) if cfg.debug else "Внутренняя ошибка сервиса"
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": detail},
        )

    return app


app = create_app()
