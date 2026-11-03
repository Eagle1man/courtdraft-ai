"""Тесты скрытия прежних дел (архив).

Отдельным файлом: это поведение появилось позже изоляции по коду
участника и отвечает на другой вопрос — «видно ли дело вообще», а не
«чьё оно».
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import AppEnv, Settings
from app.db.bootstrap import ensure_schema
from app.main import create_app
from app.models.entities import Case
from app.services.case_service import db


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    db_file = tmp_path / "archive_test.db"
    settings = Settings(
        app_env=AppEnv.DEVELOPMENT,
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        storage_dir=tmp_path / "cases",
        documents_dir=tmp_path / "cases" / "documents",
        demo_dir=tmp_path / "demo",
        laws_dir=tmp_path / "laws",
        templates_dir=tmp_path / "templates",
    )

    assert await db.connect(settings), "тесту нужна рабочая БД"
    await ensure_schema()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client
    finally:
        await db.disconnect()


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "case_number": "ARCH-001",
        "court_name": "Тестовый районный суд",
        "case_type": "civil",
        "category": "взыскание",
        "is_synthetic": True,
    }
    base.update(overrides)
    return base


async def _hide(case_id: str) -> None:
    """Пометить дело скрытым так же, как это делает миграция."""
    async with db.factory() as session:
        case = await session.get(Case, uuid.UUID(case_id))
        assert case is not None
        case.is_archived = True
        await session.commit()


class TestArchivedCases:
    """Скрытое дело отсутствует в интерфейсе полностью.

    Прежние дела не имеют владельца, поэтому фильтр по коду их не
    отсекает. Скрытие — единственный способ убрать их из списка, не
    удаляя данные.
    """

    async def _case(self, client: AsyncClient, number: str, code: str = "") -> str:
        response = await client.post(
            "/api/cases", json=_payload(case_number=number, student_code=code)
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def test_hidden_case_missing_from_list(self, client: AsyncClient) -> None:
        visible = await self._case(client, "ARCH-1")
        hidden = await self._case(client, "ARCH-2")
        await _hide(hidden)

        data = (await client.get("/api/cases")).json()
        numbers = [item["case_number"] for item in data["items"]]
        assert numbers == ["ARCH-1"]
        assert visible not in numbers

    async def test_total_excludes_hidden(self, client: AsyncClient) -> None:
        hidden = await self._case(client, "ARCH-1")
        await self._case(client, "ARCH-2")
        await _hide(hidden)

        data = (await client.get("/api/cases")).json()
        assert data["total"] == len(data["items"]) == 1

    async def test_hidden_case_returns_404_by_link(self, client: AsyncClient) -> None:
        hidden = await self._case(client, "ARCH-1")
        await _hide(hidden)

        response = await client.get(f"/api/cases/{hidden}")
        assert response.status_code == 404

    async def test_hidden_case_404_for_student_code(self, client: AsyncClient) -> None:
        hidden = await self._case(client, "ARCH-1", "S01")
        await _hide(hidden)

        response = await client.get(
            f"/api/cases/{hidden}", params={"student_code": "S01"}
        )
        assert response.status_code == 404

    async def test_hidden_documents_404(self, client: AsyncClient) -> None:
        hidden = await self._case(client, "ARCH-1")
        await _hide(hidden)

        response = await client.get(f"/api/cases/{hidden}/documents")
        assert response.status_code == 404

    async def test_created_after_archive_stays_visible(
        self, client: AsyncClient
    ) -> None:
        """Признак ставится миграцией один раз, новые дела не задеты."""
        await _hide(await self._case(client, "ARCH-OLD"))
        fresh = await self._case(client, "ARCH-NEW")

        assert (await client.get(f"/api/cases/{fresh}")).status_code == 200
        data = (await client.get("/api/cases")).json()
        assert [item["case_number"] for item in data["items"]] == ["ARCH-NEW"]
