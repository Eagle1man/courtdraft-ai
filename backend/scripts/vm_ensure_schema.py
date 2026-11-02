"""Досоздать отсутствующие таблицы в существующей SQLite-базе.

Демо-база приехала с ноутбука, где таблицы feedback ещё не было.
create_all добавляет только недостающие таблицы и не трогает данные.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import build_database_url
from app.models.entities import Base


async def main() -> None:
    url = build_database_url()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("schema ensured:", url)


if __name__ == "__main__":
    asyncio.run(main())
