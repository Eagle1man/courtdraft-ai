"""Очередь заданий: одно задание на дело, снятие дублей, закрытие зависших.

Это защита от очереди, в которой студент ждёт полчаса: каждое лишнее
нажатие раньше ставило ещё одно задание, а модель на процессоре пишет
проект минуты.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import AppEnv, Settings
from app.db.bootstrap import ensure_schema
from app.db.repositories import JobRepository
from app.main import create_app
from app.models.entities import JobRecord
from app.services.case_service import db, session_scope


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        app_env=AppEnv.DEVELOPMENT,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'queue.db').as_posix()}",
        storage_dir=tmp_path / "cases",
        documents_dir=tmp_path / "cases" / "documents",
        demo_dir=tmp_path / "demo",
        laws_dir=tmp_path / "laws",
        templates_dir=tmp_path / "templates",
        facts_enabled=True,
    )
    assert await db.connect(settings), "тесту нужна рабочая БД"
    await ensure_schema()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await db.disconnect()


async def _case(client: AsyncClient, number: str) -> str:
    created = await client.post(
        "/api/cases",
        json={
            "case_number": number,
            "court_name": "Тестовый суд",
            "case_type": "civil",
            "category": "",
            "is_synthetic": True,
        },
    )
    return created.json()["id"]


async def _add_job(case_id: str, *, kind: str = "draft", state: str = "PENDING") -> uuid.UUID:
    job_id = uuid.uuid4()
    async with session_scope(db.factory) as session:
        await JobRepository(session).create(
            job_id=job_id,
            case_id=uuid.UUID(case_id),
            state=state,
            kind=kind,
            created_at=datetime.now(UTC),
        )
    return job_id


class TestFindActive:
    async def test_pending_job_is_returned(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-1")
        job_id = await _add_job(case_id)

        async with session_scope(db.factory) as session:
            found = await JobRepository(session).find_active(
                uuid.UUID(case_id), kind="draft"
            )

        assert found is not None
        assert found.id == job_id

    async def test_finished_job_is_not_active(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-2")
        await _add_job(case_id, state="DONE")

        async with session_scope(db.factory) as session:
            found = await JobRepository(session).find_active(
                uuid.UUID(case_id), kind="draft"
            )

        assert found is None

    async def test_other_kind_is_not_active(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-3")
        await _add_job(case_id, kind="facts")

        async with session_scope(db.factory) as session:
            found = await JobRepository(session).find_active(
                uuid.UUID(case_id), kind="draft"
            )

        assert found is None

    async def test_repeated_draft_request_reuses_job(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-4")
        await client.post(
            f"/api/cases/{case_id}/documents/upload",
            files={"file": ("иск.txt", b"tekst", "text/plain")},
        )
        job_id = await _add_job(case_id)

        response = await client.post(f"/api/cases/{case_id}/draft", json={"actor": "judge"})

        assert response.status_code == 202
        assert response.json()["job_id"] == str(job_id)


class TestCancelPending:
    async def test_keeps_one_per_case(self, client: AsyncClient) -> None:
        first = await _case(client, "Q-5")
        second = await _case(client, "Q-6")
        await _add_job(first)
        await _add_job(first)
        await _add_job(first)
        await _add_job(second)

        async with session_scope(db.factory) as session:
            removed = await JobRepository(session).cancel_pending(
                kind="draft", error="уже формируется"
            )

        assert removed == 2
        async with session_scope(db.factory) as session:
            rows = await session.execute(
                select(JobRecord).where(JobRecord.state == "PENDING")
            )
            assert len(list(rows.scalars())) == 2

    async def test_cancels_all_when_one_is_running(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-7")
        await _add_job(case_id, state="RUNNING")
        await _add_job(case_id)

        async with session_scope(db.factory) as session:
            removed = await JobRepository(session).cancel_pending(
                kind="draft", error="уже формируется"
            )

        assert removed == 1

    async def test_does_not_touch_other_kinds(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-8")
        await _add_job(case_id, kind="facts")

        async with session_scope(db.factory) as session:
            removed = await JobRepository(session).cancel_pending(
                kind="draft", error="уже формируется"
            )

        assert removed == 0


class TestFailOrphaned:
    async def test_running_facts_job_is_closed(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-9")
        await _add_job(case_id, kind="facts", state="RUNNING")

        async with session_scope(db.factory) as session:
            closed = await JobRepository(session).fail_orphaned(
                kind="facts", error="служба была перезапущена"
            )

        assert closed == 1
        async with session_scope(db.factory) as session:
            job = await JobRepository(session).get(
                (await session.execute(select(JobRecord.id))).scalars().first()
            )
            assert job is not None
            assert job.state == "FAILED"
            assert "перезапущена" in job.error

    async def test_draft_jobs_are_not_touched(self, client: AsyncClient) -> None:
        case_id = await _case(client, "Q-10")
        await _add_job(case_id, kind="draft", state="RUNNING")

        async with session_scope(db.factory) as session:
            closed = await JobRepository(session).fail_orphaned(
                kind="facts", error="служба была перезапущена"
            )

        assert closed == 0
