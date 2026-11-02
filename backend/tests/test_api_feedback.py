"""Обратная связь: модель берётся из журнала, сводка делится по моделям."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import AppEnv, Settings
from app.db.bootstrap import ensure_schema
from app.db.repositories import AuditRepository
from app.main import create_app
from app.services.case_service import db, session_scope


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    db_file = tmp_path / "feedback_test.db"
    settings = Settings(
        app_env=AppEnv.DEVELOPMENT,
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        storage_dir=tmp_path / "cases",
        documents_dir=tmp_path / "cases" / "documents",
        demo_dir=tmp_path / "demo",
        laws_dir=tmp_path / "laws",
        templates_dir=tmp_path / "templates",
        llm_provider="mock",
    )
    assert await db.connect(settings)
    await ensure_schema()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client
    finally:
        await db.disconnect()


async def _case(client: AsyncClient, number: str = "FB-001") -> uuid.UUID:
    response = await client.post(
        "/api/cases",
        json={
            "case_number": number,
            "court_name": "Тестовый суд",
            "case_type": "civil",
            "is_synthetic": True,
            "student_code": "S01",
        },
    )
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


async def test_feedback_uses_model_from_generation_audit(client: AsyncClient) -> None:
    case_id = await _case(client)
    async with session_scope(db.factory) as session:
        await AuditRepository(session).log(
            "draft.generated",
            case_id=case_id,
            actor="judge",
            provider="ollama",
            model="strong:7b",
        )

    response = await client.post(
        "/api/feedback",
        json={
            "case_id": str(case_id),
            "rating": 4,
            "verdict": "NEEDS_WORK",
            "comment": "Проверка ярлыка модели",
            "author_role": "student",
            "author_name": "S01",
        },
    )

    assert response.status_code == 201
    assert response.json()["model_name"] == "ollama:strong:7b"


async def test_stats_are_split_by_model(client: AsyncClient) -> None:
    first = await _case(client, "FB-001")
    second = await _case(client, "FB-002")
    async with session_scope(db.factory) as session:
        audit = AuditRepository(session)
        await audit.log(
            "draft.generated",
            case_id=first,
            actor="judge",
            provider="ollama",
            model="strong:7b",
        )
        await audit.log(
            "draft.generated",
            case_id=second,
            actor="judge",
            provider="ollama",
            model="weak:1.5b",
        )

    for case_id, rating in ((first, 5), (second, 2)):
        response = await client.post(
            "/api/feedback",
            json={
                "case_id": str(case_id),
                "rating": rating,
                "verdict": "ACCEPTED" if rating > 3 else "REJECTED",
                "author_role": "student",
            },
        )
        assert response.status_code == 201

    stats = (await client.get("/api/feedback/stats")).json()

    assert stats["total"] == 2
    assert stats["by_model"]["ollama:strong:7b"] == {
        "count": 1,
        "average_rating": 5.0,
    }
    assert stats["by_model"]["ollama:weak:1.5b"] == {
        "count": 1,
        "average_rating": 2.0,
    }


async def test_student_code_is_kept_in_case_audit(client: AsyncClient) -> None:
    case_id = await _case(client, "FB-003")
    async with session_scope(db.factory) as session:
        entries = await AuditRepository(session).list_for_case(case_id)
    created = [entry for entry in entries if entry.action == "case.created"]
    assert created
    assert created[0].details["student_code"] == "S01"
