"""Проверка инфраструктуры: PostgreSQL и Qdrant.

Зачем: на машине разработки и на ВМ разный набор сервисов, и «приложение
стартует» не значит «оно работает на PostgreSQL». Скрипт отвечает
по-человечески на три вопроса:

1. какой источник данных реально используется (SQLite или PostgreSQL);
2. отвечает ли Qdrant и какие коллекции в нём есть;
3. созданы ли таблицы (то есть прошли ли миграции).

Ничего не меняет и не мигрирует: миграции — отдельное решение.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\infra_check.py

Коды: 0 — инфраструктура в порядке или явно отключена; 2 — что-то
настроено, но не отвечает (это и есть повод не запускать прогон).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import QdrantMode, Settings  # noqa: E402
from app.db.session import build_database_url, describe_url  # noqa: E402


async def check_database(settings: Settings) -> tuple[bool, str]:
    url = build_database_url(settings)
    if url.startswith("sqlite"):
        return True, f"источник данных: SQLite ({describe_url(url)}) — не PostgreSQL"
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            version = (await connection.execute(text("SELECT version()"))).scalar()
            tables = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001 - отчёт должен назвать причину
        return False, f"PostgreSQL не отвечает: {exc}"
    finally:
        await engine.dispose()
    short = str(version).split(",")[0]
    return True, f"PostgreSQL отвечает ({short}); таблиц в public: {tables}"


async def check_qdrant(settings: Settings) -> tuple[bool, str]:
    if settings.qdrant_mode is not QdrantMode.SERVER:
        return True, f"Qdrant в режиме {settings.qdrant_mode.value} — внешний сервер не нужен"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{settings.qdrant_url.rstrip('/')}/collections")
            response.raise_for_status()
            collections = response.json().get("result", {}).get("collections", [])
    except httpx.HTTPError as exc:
        return False, f"Qdrant не отвечает ({settings.qdrant_url}): {exc}"
    names = ", ".join(item.get("name", "") for item in collections) or "коллекций нет"
    return True, f"Qdrant отвечает, коллекции: {names}"


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = Settings()
    print(f"режим приложения: {settings.app_env.value}")
    print(f"LLM: {settings.llm_provider.value}, эмбеддинги: {settings.embedding_provider.value}")

    results = [
        await check_database(settings),
        await check_qdrant(settings),
    ]
    failed = 0
    for ok, message in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {message}")
        failed += 0 if ok else 1

    if failed:
        print("НЕ ГОТОВО: поднимите сервисы (docker compose up -d postgres qdrant)")
        return 2
    print("ПРОШЛО: инфраструктура отвечает")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
