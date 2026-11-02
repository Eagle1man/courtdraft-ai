"""Тесты API дел и документов (Фаза 3, п.42).

Приложение собирается с БД на временной SQLite, поэтому тесты не требуют
ни PostgreSQL, ни Docker (п.50).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import AppEnv, Settings
from app.db.bootstrap import ensure_schema
from app.main import create_app
from app.services.case_service import db


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Клиент с подключённой временной БД."""
    db_file = tmp_path / "api_test.db"

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
        "case_number": "DEMO-001",
        "court_name": "Тестовый районный суд",
        "case_type": "civil",
        "category": "взыскание",
        "is_synthetic": True,
    }
    base.update(overrides)
    return base


class TestCreateCase:
    async def test_create_returns_201(self, client: AsyncClient) -> None:
        response = await client.post("/api/cases", json=_payload())
        assert response.status_code == 201

        data = response.json()
        assert data["case_number"] == "DEMO-001"
        assert data["status"] == "NEW"
        assert data["is_synthetic"] is True

    async def test_duplicate_number_returns_409(self, client: AsyncClient) -> None:
        await client.post("/api/cases", json=_payload())
        response = await client.post("/api/cases", json=_payload())
        assert response.status_code == 409

    async def test_missing_required_field_returns_422(self, client: AsyncClient) -> None:
        response = await client.post("/api/cases", json={"case_number": "X"})
        assert response.status_code == 422

    async def test_unknown_field_rejected(self, client: AsyncClient) -> None:
        """extra=forbid: опечатка в поле должна быть видна, а не проигнорирована."""
        response = await client.post("/api/cases", json=_payload(typo_field="x"))
        assert response.status_code == 422


class TestListCases:
    async def test_empty_list(self, client: AsyncClient) -> None:
        response = await client.get("/api/cases")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0}

    async def test_list_after_create(self, client: AsyncClient) -> None:
        await client.post("/api/cases", json=_payload())
        data = (await client.get("/api/cases")).json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    async def test_filter_by_status(self, client: AsyncClient) -> None:
        created = await client.post("/api/cases", json=_payload())
        case_id = created.json()["id"]
        await client.patch(f"/api/cases/{case_id}", json={"status": "DRAFT_READY"})

        ready = (await client.get("/api/cases", params={"status": "DRAFT_READY"})).json()
        assert ready["total"] == 1

        new = (await client.get("/api/cases", params={"status": "NEW"})).json()
        assert new["total"] == 0

    async def test_invalid_status_filter_returns_422(self, client: AsyncClient) -> None:
        response = await client.get("/api/cases", params={"status": "ВЫДУМКА"})
        assert response.status_code == 422

    async def test_pagination_limits(self, client: AsyncClient) -> None:
        response = await client.get("/api/cases", params={"limit": 500})
        assert response.status_code == 422


class TestGetCase:
    async def test_existing(self, client: AsyncClient) -> None:
        created = (await client.post("/api/cases", json=_payload())).json()
        response = await client.get(f"/api/cases/{created['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    async def test_missing_returns_404(self, client: AsyncClient) -> None:
        response = await client.get("/api/cases/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    async def test_invalid_uuid_returns_422(self, client: AsyncClient) -> None:
        response = await client.get("/api/cases/не-uuid")
        assert response.status_code == 422


class TestUpdateCase:
    async def test_change_status(self, client: AsyncClient) -> None:
        created = (await client.post("/api/cases", json=_payload())).json()
        response = await client.patch(
            f"/api/cases/{created['id']}", json={"status": "UNDER_REVIEW"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "UNDER_REVIEW"

    async def test_invalid_status_rejected(self, client: AsyncClient) -> None:
        created = (await client.post("/api/cases", json=_payload())).json()
        response = await client.patch(
            f"/api/cases/{created['id']}", json={"status": "ВЫДУМКА"}
        )
        assert response.status_code == 422

    async def test_update_missing_case_returns_404(self, client: AsyncClient) -> None:
        response = await client.patch(
            "/api/cases/00000000-0000-0000-0000-000000000000",
            json={"category": "x"},
        )
        assert response.status_code == 404


class TestDeleteCase:
    async def _case(self, client: AsyncClient) -> str:
        resp = await client.post("/api/cases", json=_payload())
        data = resp.json()
        return data["id"]

    async def test_delete_case_returns_204_and_removes_from_list(self, client: AsyncClient) -> None:
        case_id = await self._case(client)
        response = await client.delete(f"/api/cases/{case_id}")
        assert response.status_code == 204
        assert response.text == ""

        listed = (await client.get("/api/cases")).json()
        assert listed["total"] == 0
        assert listed["items"] == []

        get_resp = await client.get(f"/api/cases/{case_id}")
        assert get_resp.status_code == 404

    async def test_delete_missing_case_returns_404(self, client: AsyncClient) -> None:
        response = await client.delete("/api/cases/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        assert response.json()["detail"] == "Дело не найдено"

    async def test_delete_cascade_documents_and_files(self, client: AsyncClient, tmp_path: Path) -> None:
        case_id = await self._case(client)
        case_dir = tmp_path / "cases" / "originals" / case_id
        case_dir.mkdir(parents=True)
        original = case_dir / "x.txt"
        original.write_text("содержимое", encoding="utf-8")

        resp = await client.delete(f"/api/cases/{case_id}")
        assert resp.status_code == 204
        assert not original.exists()
        assert not case_dir.exists()
    async def _case(self, client: AsyncClient) -> str:
        resp = await client.post("/api/cases", json=_payload())
        data = resp.json()
        return data["id"]

    async def test_register_document(self, client: AsyncClient) -> None:
        case_id = await self._case(client)
        response = await client.post(
            f"/api/cases/{case_id}/documents",
            json={
                "filename": "01_claim.pdf",
                "document_type": "CLAIM",
                "mime_type": "application/pdf",
                "page_count": 3,
                "text": "Исковое заявление",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["filename"] == "01_claim.pdf"
        assert data["is_known_type"] is True
        assert len(data["sha256"]) == 64

    async def test_list_documents(self, client: AsyncClient) -> None:
        case_id = await self._case(client)
        for name, dtype in (("a.pdf", "CLAIM"), ("b.pdf", "PROTOCOL")):
            await client.post(
                f"/api/cases/{case_id}/documents",
                json={"filename": name, "document_type": dtype, "text": name},
            )

        docs = (await client.get(f"/api/cases/{case_id}/documents")).json()
        assert len(docs) == 2

    async def test_filter_by_type(self, client: AsyncClient) -> None:
        case_id = await self._case(client)
        await client.post(
            f"/api/cases/{case_id}/documents",
            json={"filename": "a.pdf", "document_type": "CLAIM", "text": "one"},
        )
        await client.post(
            f"/api/cases/{case_id}/documents",
            json={"filename": "b.pdf", "document_type": "PROTOCOL", "text": "two"},
        )

        claims = (
            await client.get(f"/api/cases/{case_id}/documents", params={"type": "CLAIM"})
        ).json()
        assert len(claims) == 1

    async def test_duplicate_content_returns_409(self, client: AsyncClient) -> None:
        """SHA256 защищает от загрузки одного файла дважды."""
        case_id = await self._case(client)
        body = {"filename": "a.pdf", "document_type": "CLAIM", "text": "одинаковый текст"}

        assert (await client.post(f"/api/cases/{case_id}/documents", json=body)).status_code == 201
        assert (await client.post(f"/api/cases/{case_id}/documents", json=body)).status_code == 409

    async def test_unknown_document_type_allowed(self, client: AsyncClient) -> None:
        """п.11: типы расширяемы — новое значение принимается, но помечается."""
        case_id = await self._case(client)
        response = await client.post(
            f"/api/cases/{case_id}/documents",
            json={"filename": "x.pdf", "document_type": "НОВЫЙ_ТИП", "text": "x"},
        )
        assert response.status_code == 201
        assert response.json()["is_known_type"] is False

    async def test_document_for_missing_case_returns_404(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/api/cases/00000000-0000-0000-0000-000000000000/documents",
            json={"filename": "a.pdf", "text": "x"},
        )
        assert response.status_code == 404

    async def test_case_shows_document_count(self, client: AsyncClient) -> None:
        """Счётчик документов в карточке дела (п.34)."""
        case_id = await self._case(client)
        await client.post(
            f"/api/cases/{case_id}/documents",
            json={"filename": "a.pdf", "text": "x"},
        )
        data = (await client.get(f"/api/cases/{case_id}")).json()
        assert data["document_count"] == 1


class TestHealthWithDatabase:
    async def test_database_reported_ok(self, client: AsyncClient) -> None:
        """БД проверяется реальным запросом, а не открытым портом."""
        data = (await client.get("/api/health")).json()
        by_name = {c["name"]: c for c in data["components"]}
        assert by_name["Database"]["state"] == "OK"

    async def test_sqlite_named_honestly(self, client: AsyncClient) -> None:
        """health не должен называть SQLite PostgreSQL."""
        data = (await client.get("/api/health")).json()
        by_name = {c["name"]: c for c in data["components"]}
        assert "SQLite" in by_name["Database"]["detail"]

    async def test_overall_status_is_ok(self, client: AsyncClient) -> None:
        """С работающей БД статус поднимается с DEGRADED до OK.

        Проверка статуса целиком зависит от всех компонентов, поэтому
        тест задаёт провайдеры заглушками. Иначе результат зависел бы
        от .env машины разработчика: переключили провайдера на реальную
        модель — и тест падал на том, что модель на ноутбуке не
        запущена. Проверка БД тут ни при чём.
        """
        from app.config import EmbeddingProviderName, LLMProviderName, Settings
        from app.main import create_app

        settings = Settings(
            llm_provider=LLMProviderName.MOCK,
            embedding_provider=EmbeddingProviderName.MOCK,
            database_url="sqlite+aiosqlite:///:memory:",
        )
        transport = ASGITransport(app=create_app(settings))
        async with AsyncClient(transport=transport, base_url="http://test") as own:
            data = (await own.get("/api/health")).json()

        assert data["status"] == "OK"
