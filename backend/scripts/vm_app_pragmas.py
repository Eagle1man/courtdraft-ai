"""Проверить PRAGMA на соединении приложения, а не на сыром sqlite3."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.session import create_engine_and_session


async def main() -> None:
    async with create_engine_and_session() as (engine, _factory):
        async with engine.connect() as connection:
            for pragma in ("journal_mode", "busy_timeout", "foreign_keys"):
                value = (
                    await connection.execute(text(f"PRAGMA {pragma}"))
                ).scalar()
                print(pragma, value)


if __name__ == "__main__":
    asyncio.run(main())
