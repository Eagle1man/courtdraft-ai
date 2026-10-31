"""Смысловой фильтр инструкций: разбиение, порог и поведение при сбое.

Проверяется то, что можно проверить без скачанной модели: окна по 512
токенов, порог, и главное — включённый, но недоступный фильтр не
отклоняет документ и не делает вид, что проверка была.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.session import create_all, create_engine_and_session, drop_all
from app.embeddings.mock import MockEmbeddingProvider
from app.ingestion import injection_guard
from app.ingestion.injection_guard import (
    CHUNK_CHARS,
    GuardUnavailableError,
    GuardVerdict,
    _split,
    guard_check,
)
from app.ingestion.pipeline import ingest_document
from app.models.entities import Case, CaseStatus
from app.rag.store import VectorStore


class TestSplit:
    def test_short_text_is_one_chunk(self) -> None:
        assert _split("короткий текст") == ["короткий текст"]

    def test_long_text_is_split_with_overlap(self) -> None:
        chunks = _split("а" * (CHUNK_CHARS * 2))
        assert len(chunks) >= 2
        assert all(len(chunk) <= CHUNK_CHARS for chunk in chunks)

    def test_empty_text_has_no_chunks(self) -> None:
        assert _split("   ") == []


class TestGuardCheck:
    async def test_high_score_is_suspicious(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(injection_guard, "availability", lambda model: (True, ""))
        monkeypatch.setattr(
            injection_guard, "classify_chunks", lambda chunks, model: [0.95]
        )
        verdict = await guard_check("текст", model="test")
        assert verdict.suspicious is True
        assert verdict.score == 0.95

    async def test_low_score_is_not_suspicious(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(injection_guard, "availability", lambda model: (True, ""))
        monkeypatch.setattr(
            injection_guard, "classify_chunks", lambda chunks, model: [0.05]
        )
        verdict = await guard_check("текст", model="test")
        assert verdict.suspicious is False

    async def test_model_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(chunks: list[str], model: str) -> list[float]:
            raise RuntimeError("модель не скачана")

        monkeypatch.setattr(injection_guard, "availability", lambda model: (True, ""))
        monkeypatch.setattr(injection_guard, "classify_chunks", broken)
        with pytest.raises(GuardUnavailableError, match="не скачана"):
            await guard_check("текст", model="test")


class TestPipelineWiring:
    @pytest_asyncio.fixture
    async def session(self, tmp_path: Path):  # noqa: ANN201
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{(tmp_path / 'guard.db').as_posix()}"
        )
        manager = create_engine_and_session(settings)
        engine, _ = await manager.__aenter__()
        await create_all(engine)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as sess:
            case = Case(
                case_number="GUARD-1",
                court_name="Суд",
                case_type="civil",
                status=CaseStatus.NEW.value,
            )
            sess.add(case)
            await sess.commit()
            yield sess, case
        await drop_all(engine)
        await manager.__aexit__(None, None, None)

    async def _ingest(self, sess, case, settings: Settings):  # noqa: ANN001, ANN202
        from qdrant_client import QdrantClient

        store = VectorStore(QdrantClient(":memory:"), dimension=64)
        return await ingest_document(
            sess,
            case.id,
            "01_claim.txt",
            "ИСКОВОЕ ЗАЯВЛЕНИЕ. Взыскать задолженность 150 000 руб.".encode(),
            store=store,
            embeddings=MockEmbeddingProvider(dimension=64),
            settings=settings,
        )

    def _settings(self) -> Settings:
        return Settings(
            injection_guard_enabled=True,
            injection_guard_model="test-guard",
        )

    async def test_suspicious_document_gets_warning(
        self, session, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        sess, case = session

        async def fake_guard(text: str, *, model: str) -> GuardVerdict:
            return GuardVerdict(score=0.93, model=model, chunks=1)

        monkeypatch.setattr(injection_guard, "guard_check", fake_guard)
        result = await self._ingest(sess, case, self._settings())
        await sess.commit()

        assert any("фильтр" in warning.lower() for warning in result.warnings), (
            result.warnings
        )

    async def test_unavailable_guard_does_not_block_upload(
        self, session, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        sess, case = session

        async def broken(text: str, *, model: str) -> GuardVerdict:
            raise GuardUnavailableError("нет transformers/torch")

        monkeypatch.setattr(injection_guard, "guard_check", broken)
        result = await self._ingest(sess, case, self._settings())
        await sess.commit()

        assert result.character_count > 0, "документ обязан сохраниться"
        assert any("недоступен" in warning for warning in result.warnings)

    async def test_guard_disabled_by_default(self, session) -> None:  # noqa: ANN001
        sess, case = session
        result = await self._ingest(sess, case, Settings())
        await sess.commit()
        assert not any("фильтр" in warning.lower() for warning in result.warnings)
