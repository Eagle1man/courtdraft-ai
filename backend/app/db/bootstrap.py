"""Создание схемы БД при старте (Фаза 3).

Зачем это нужно рядом с Alembic:

* на машине разработки PostgreSQL нет, и первый запуск не должен
  требовать ручного `alembic upgrade head`;
* демо (`demo.ps1`, п.41) обязано подниматься одной командой.

В боеющем развёртывании схему создаёт Alembic, а не этот модуль:
`create_all` не знает о прежних версиях схемы и не умеет откат.
Поэтому функция применяется только когда Alembic ещё не проставлял
отметку в этой базе.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from app.db.session import create_all
from app.models.entities import Base
from app.services.case_service import db

logger = logging.getLogger(__name__)

ALEMBIC_VERSION_TABLE = "alembic_version"


async def schema_needs_creation() -> bool:
    """Проверить, пустая ли база.

    Если Alembic уже проставлял версию, схема под управлением миграций —
    трогать её нельзя.
    """
    engine = db.engine
    if engine is None:
        return False

    def _inspect(sync_conn: Connection) -> tuple[bool, list[str]]:
        tables = list(inspect(sync_conn).get_table_names())
        return bool(tables), tables

    async with engine.connect() as connection:
        has_tables, tables = await connection.run_sync(_inspect)

    if ALEMBIC_VERSION_TABLE in tables:
        logger.info("Схема под управлением Alembic, create_all пропущен")
        return False

    return not has_tables


async def ensure_schema() -> bool:
    """Создать таблицы, если база пуста.

    Возвращает False, если создавать нечего либо схема уже под Alembic.
    """
    try:
        if not await schema_needs_creation():
            return False
        if db.engine is None:
            return False
        await create_all(db.engine)
        logger.info(
            "Создано %d таблиц: %s",
            len(Base.metadata.tables),
            ", ".join(sorted(Base.metadata.tables)),
        )
        return True
    except Exception:  # noqa: BLE001
        # Недоступность БД не должна ронять старт: система сообщит
        # о ней в /api/health и продолжит работать degraded.
        logger.warning("Не удалось создать схему БД", exc_info=True)
        return False


async def check_schema_drift() -> list[str]:
    """Найти колонки, которых нет в базе, но есть в моделях.

    Зачем: `create_all` только создаёт таблицы и никогда не меняет уже
    существующие. База, созданная до появления нового поля, продолжает
    работать — и падает позже, внутри фоновой задачи, сообщением
    «no column named ...», которое ничего не объясняет.

    Проверка при старте превращает это в понятное сообщение с точной
    командой исправления.
    """
    engine = db.engine
    if engine is None:
        return []

    def _inspect(sync_conn: Connection) -> list[str]:
        inspector = inspect(sync_conn)
        missing: list[str] = []

        for table_name, table in Base.metadata.tables.items():
            if table_name not in inspector.get_table_names():
                # Отсутствующая таблица — не рассинхрон, её создаст Alembic.
                continue
            actual = {c["name"] for c in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name not in actual:
                    missing.append(f"{table_name}.{column.name}")
        return missing

    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_inspect)
    except Exception:  # noqa: BLE001
        logger.warning("Не удалось проверить схему БД", exc_info=True)
        return []


MIGRATE_HINT = (
    "База отстаёт от моделей. Обновите её командой:\n"
    "  python -m alembic stamp 4a19f2c7e8b1\n"
    "  python -m alembic upgrade head\n"
    "\n"
    "Либо удалите файл базы и перезапустите сервис: данные будут потеряны."
)
