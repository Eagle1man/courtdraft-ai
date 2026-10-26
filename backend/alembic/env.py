"""Окружение Alembic.

URL берётся из настроек приложения (app.config), а не из alembic.ini.
Причина: иначе миграции применялись бы к зашитой в конфиг базе, а не к
той, что реально настроена в .env — и перенос на другую машину ломался бы
молча (п.61).

Работает и с PostgreSQL, и с SQLite: на DEV-машине PostgreSQL нет.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.db.session import build_database_url, is_sqlite
from app.models.entities import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or build_database_url(get_settings())


def run_migrations_offline() -> None:
    """Миграции без подключения к БД (генерация SQL)."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite не умеет ALTER COLUMN через batch, но умеет batch_alter_table.
        render_as_batch=is_sqlite(url),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    """Общий шаг для offline и online."""
    url = _database_url()
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        render_as_batch=is_sqlite(url),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Миграции с подключением к БД."""
    url = _database_url()
    engine = create_async_engine(url, future=True, echo=get_settings().debug)

    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online_sync() -> None:
    """Синхронная обёртка для Alembic."""
    asyncio.run(run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online_sync()
