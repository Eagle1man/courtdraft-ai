"""Подключение к БД (Фаза 3).

Ключевое решение: слой работает и с PostgreSQL, и с SQLite.

Причина — ограничение машины разработки: PostgreSQL не установлен, Docker
недоступен (Docker Toolbox 2019 с мёртвым демоном), а C: имеет 1.2 GB.
Заставлять разработку ждать установки БД означало бы остановить работу
над Фазами 3–10.

Следствия, которые важны:
* тесты гоняются на SQLite — быстро и без внешних сервисов;
* на машине с RTX 3050 достаточно поменять DATABASE_URL в .env;
* код при этом НЕ меняется (п.61).

Оба диалекта поддерживаются одинаково: в моделях нет ни `JSONB`,
ни нативных `ENUM`, ни `SERIAL` — только переносимые типы.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.models.entities import Base

logger = logging.getLogger(__name__)


def build_database_url(settings: Settings | None = None) -> str:
    """Определить URL подключения.

    Приоритет:
      1. явный `DATABASE_URL` в .env;
      2. в режиме development с не-настроенным PostgreSQL — SQLite
         (на ноутбуке сервера нет, и падать при старте хуже);
      3. иначе URL собирается из `POSTGRES_*`.
    """
    cfg = settings or get_settings()

    explicit = cfg.database_url.strip()
    if explicit:
        return explicit

    if cfg.app_env.value == "development" and _looks_unconfigured(cfg):
        return f"sqlite+aiosqlite:///{_sqlite_path(cfg)}"

    return (
        f"postgresql+asyncpg://{cfg.postgres_user}:{cfg.postgres_password}"
        f"@{cfg.postgres_host}:{cfg.postgres_port}/{cfg.postgres_db}"
    )


def _looks_unconfigured(cfg: Settings) -> bool:
    """Похоже, что пользователь не настраивал PostgreSQL.

    Сравнение с дефолтами из .env.example. Если значения вернулись к
    дефолтным, значит БД не настраивали — на DEV разумнее SQLite,
    чем падение при подключении к несуществующему серверу.
    """
    defaults = Settings.model_fields
    default_user = defaults["postgres_user"].default
    default_password = defaults["postgres_password"].default
    return bool(
        cfg.postgres_user == default_user and cfg.postgres_password == default_password
    )


def _sqlite_path(cfg: Settings) -> str:
    """Путь к файлу SQLite в каталоге данных.

    Требуется абсолютный путь и прямые слэши: URL вида
    `sqlite:///data/cases/x.db` SQLAlchemy читает как путь от корня
    диска, и база оказывается не там, где каталог данных.
    """
    cfg.storage_dir.mkdir(parents=True, exist_ok=True)
    return (cfg.storage_dir / "courtdraft.db").as_posix()


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


@asynccontextmanager
async def create_engine_and_session(
    settings: Settings | None = None,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Создать движок и фабрику сессий (Фаза 3).

    Используется как менеджер контекста, чтобы гарантированно закрыть
    соединения — на Windows незакрытый пул держит файлы БД.
    """
    cfg = settings or get_settings()
    url = build_database_url(cfg)

    kwargs: dict[str, object] = {"echo": cfg.debug, "future": True}
    if is_sqlite(url):
        # SQLite не переносит параллельные записи так, как PostgreSQL.
        # Ограничение нужно, а не «на всякий случай».
        kwargs["connect_args"] = {"timeout": 60}
    else:
        # На ноутбуке 8 GB лишние соединения PostgreSQL съедают память.
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 5
        kwargs["pool_pre_ping"] = True
        kwargs["pool_recycle"] = 1800

    engine = create_async_engine(url, **kwargs)

    if is_sqlite(url):
        _configure_sqlite(engine)

    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    try:
        yield engine, factory
    finally:
        await engine.dispose()


def _configure_sqlite(engine: AsyncEngine) -> None:
    """Включить внешние ключи для SQLite.

    Без этого ON DELETE CASCADE из моделей не работает, и удаление дела
    оставит осиротевшие документы. SQLite по умолчанию игнорирует FK.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: object, _record: object) -> None:
        # Транзакциями управляет SQLAlchemy через событие "begin" ниже.
        # Без этого sqlite3 начинает обычную DEFERRED-транзакцию, и
        # апгрейд «чтение -> запись» при конкуренции падает с
        # «database is locked» сразу, не дожидаясь busy_timeout.
        dbapi_connection.isolation_level = None  # type: ignore[attr-defined]
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL: читатели не ждут писателя. Без этого список дел и проверка
        # состояния встают на время записи проекта.
        cursor.execute("PRAGMA journal_mode=WAL")
        # NORMAL достаточно: WAL уже даёт целостность при сбое процесса,
        # а FULL на каждый коммит замедляет загрузку документов.
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Очередь писателей: ждать, а не падать с «database is locked».
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _begin_immediate(connection: object) -> None:
        # BEGIN IMMEDIATE берёт блокировку записи сразу. Иначе транзакция
        # начинается как читающая, а при первом INSERT пытается стать
        # пишущей; если между этими моментами записал кто-то другой,
        # SQLite отдаёт SQLITE_BUSY немедленно и busy_timeout не спасает.
        connection.exec_driver_sql("BEGIN IMMEDIATE")  # type: ignore[attr-defined]


async def create_all(engine: AsyncEngine) -> None:
    """Создать все таблицы.

    Используется в тестах и демо. Боевое развёртывание идёт через
    миграции Alembic: create_all не знает о прежних версиях схемы.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("Схема БД создана: %d таблиц", len(Base.metadata.tables))


async def drop_all(engine: AsyncEngine) -> None:
    """Удалить все таблицы. Только для тестов."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def check_connection(engine: AsyncEngine) -> bool:
    """Проверить живость соединения (для /api/health)."""
    from sqlalchemy import text

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health-check не должен бросать
        logger.debug("Проверка соединения с БД не удалась", exc_info=True)
        return False
    return True


def describe_url(url: str) -> str:
    """Безопасное описание URL для логов: без пароля."""
    if is_sqlite(url):
        return f"sqlite:{urlsplit(url).path}"
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}{parts.path}"
