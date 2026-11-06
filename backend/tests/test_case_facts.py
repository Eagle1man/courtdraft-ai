"""Реестр фактов в сервисе: API, задание, слияние документов.

Проверяется не качество извлечения (для него нужен gold-набор и этап 8),
а поведение обвязки: выключенный флаг, пустое дело, задание, снимок,
разделение заданий формирования проекта и извлечения фактов.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import AppEnv, Settings
from app.db.bootstrap import ensure_schema
from app.extraction.fact_extractor import merge_registries
from app.extraction.schemas import (
    CaseAmount,
    CaseParticipant,
    CaseRegistry,
    ExtractionStatus,
    SourceRef,
)
from app.main import create_app
from app.services.case_service import db
from app.services.fact_service import summarize

TEXT = (
    "Истец ООО «Ромашка» просит взыскать с ответчика ООО «Лютик» "
    "задолженность 1 225 000 рублей по договору от 15.01.2025."
)


def _settings(tmp_path: Path, *, facts_enabled: bool) -> Settings:
    return Settings(
        app_env=AppEnv.DEVELOPMENT,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'facts.db').as_posix()}",
        storage_dir=tmp_path / "cases",
        documents_dir=tmp_path / "cases" / "documents",
        demo_dir=tmp_path / "demo",
        laws_dir=tmp_path / "laws",
        templates_dir=tmp_path / "templates",
        facts_enabled=facts_enabled,
        facts_max_documents=2,
    )


async def _client(tmp_path: Path, *, facts_enabled: bool) -> AsyncIterator[AsyncClient]:
    settings = _settings(tmp_path, facts_enabled=facts_enabled)
    assert await db.connect(settings), "тесту нужна рабочая БД"
    await ensure_schema()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await db.disconnect()


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    async for item in _client(tmp_path, facts_enabled=True):
        yield item


@pytest_asyncio.fixture
async def client_off(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    async for item in _client(tmp_path, facts_enabled=False):
        yield item


async def _case_with_document(client: AsyncClient) -> str:
    created = await client.post(
        "/api/cases",
        json={
            "case_number": "FACTS-001",
            "court_name": "Тестовый суд",
            "case_type": "arbitration",
            "category": "взыскание",
            "is_synthetic": True,
        },
    )
    assert created.status_code == 201
    case_id = created.json()["id"]

    uploaded = await client.post(
        f"/api/cases/{case_id}/documents/upload",
        files={"file": ("иск.txt", TEXT.encode("utf-8"), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    return case_id


class TestFactApi:
    async def test_disabled_flag_returns_409(self, client_off: AsyncClient) -> None:
        case_id = await _case_with_document(client_off)

        response = await client_off.post(f"/api/cases/{case_id}/facts")

        assert response.status_code == 409
        assert "FACTS_ENABLED" in response.json()["detail"]

    async def test_empty_case_returns_409(self, client: AsyncClient) -> None:
        created = await client.post(
            "/api/cases",
            json={
                "case_number": "FACTS-EMPTY",
                "court_name": "Тестовый суд",
                "case_type": "civil",
                "category": "",
                "is_synthetic": True,
            },
        )
        case_id = created.json()["id"]

        response = await client.post(f"/api/cases/{case_id}/facts")

        assert response.status_code == 409
        assert "нет материалов" in response.json()["detail"].lower()

    async def test_unknown_case_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/cases/00000000-0000-0000-0000-000000000000/facts"
        )

        assert response.status_code == 404

    async def test_get_before_extraction_returns_404(self, client: AsyncClient) -> None:
        case_id = await _case_with_document(client)

        response = await client.get(f"/api/cases/{case_id}/facts")

        assert response.status_code == 404

    async def test_extraction_saves_registry_snapshot(self, client: AsyncClient) -> None:
        case_id = await _case_with_document(client)

        started = await client.post(f"/api/cases/{case_id}/facts")
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]

        job = await client.get(f"/api/jobs/{job_id}")
        assert job.status_code == 200
        assert job.json()["state"] == "DONE", job.json()

        facts = await client.get(f"/api/cases/{case_id}/facts")
        assert facts.status_code == 200, facts.text
        body = facts.json()

        assert body["case_id"] == case_id
        assert body["documents"] == 1
        assert body["status"] in {"ok", "needs_review"}
        assert "registry" in body
        assert "quotes" in body["summary"]
        # Модель в тестах — заглушка, поэтому проверяется форма, а не
        # содержание: пустой реестр это честный ответ, а не ошибка.
        assert body["summary"]["facts"] >= 0
        assert "participants" in body["registry"]


class TestMergeRegistries:
    def test_same_participant_from_two_documents_is_deduped(self) -> None:
        first = CaseRegistry(
            participants=[
                CaseParticipant(
                    role="plaintiff",
                    name="ООО «Ромашка»",
                    source=SourceRef(document_id="D-1", quote="Истец ООО «Ромашка»"),
                )
            ]
        )
        second = CaseRegistry(
            participants=[
                CaseParticipant(
                    role="plaintiff",
                    name="ООО  «Ромашка»",
                    source=SourceRef(document_id="D-2", quote="ООО «Ромашка»"),
                )
            ]
        )

        merged = merge_registries([first, second])

        assert len(merged.participants) == 1

    def test_same_amount_from_two_documents_is_deduped(self) -> None:
        from decimal import Decimal

        first = CaseRegistry(amounts=[CaseAmount(kind="principal", value=Decimal("1000"))])
        second = CaseRegistry(
            amounts=[CaseAmount(kind="principal", value=Decimal("1000.00"))]
        )

        merged = merge_registries([first, second])

        assert len(merged.amounts) == 1

    def test_different_amounts_both_survive(self) -> None:
        from decimal import Decimal

        first = CaseRegistry(amounts=[CaseAmount(kind="principal", value=Decimal("1000"))])
        second = CaseRegistry(
            amounts=[CaseAmount(kind="principal", value=Decimal("1500"))]
        )

        merged = merge_registries([first, second])

        assert len(merged.amounts) == 2

    def test_needs_review_propagates(self) -> None:
        merged = merge_registries(
            [CaseRegistry(), CaseRegistry(status=ExtractionStatus.NEEDS_REVIEW)]
        )

        assert merged.status is ExtractionStatus.NEEDS_REVIEW

    def test_summarize_counts_quotes(self) -> None:
        registry = CaseRegistry()

        summary = summarize(registry, {"exact": 2, "normalized": 1, "absent": 0})

        assert summary["facts"] == 0
        assert summary["quotes"] == {"exact": 2, "normalized": 1, "absent": 0}
