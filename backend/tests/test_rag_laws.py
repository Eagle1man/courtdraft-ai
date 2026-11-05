"""База знаний по нормам: индексация, поиск, постоянное хранилище."""

from __future__ import annotations

from pathlib import Path

from qdrant_client import QdrantClient

from app.config import QdrantMode, Settings
from app.datasets.law_corpus import LawChunk
from app.embeddings.mock import MockEmbeddingProvider
from app.rag.laws import (
    format_law_context,
    index_law_chunks,
    search_laws,
)
from app.rag.store import CollectionName, VectorStore

CHUNKS = [
    LawChunk(
        chunk_id="ГК РФ:art395:000",
        code="ГК РФ",
        article="395",
        title="Ответственность за неисполнение денежного обязательства",
        part="Часть первая",
        text="За пользование чужими денежными средствами подлежат уплате проценты.",
    ),
    LawChunk(
        chunk_id="ГК РФ:art309:000",
        code="ГК РФ",
        article="309",
        title="Общие положения",
        part="Часть первая",
        text="Обязательства должны исполняться надлежащим образом.",
    ),
    LawChunk(
        chunk_id="ГПК РФ:art56:000",
        code="ГПК РФ",
        article="56",
        title="Обязанность доказывания",
        part="",
        text="Каждая сторона должна доказать обстоятельства, на которые ссылается.",
    ),
]


class TestIndexing:
    async def test_chunks_are_indexed(self) -> None:
        store = VectorStore(QdrantClient(":memory:"), dimension=1024)
        written = await index_law_chunks(store, MockEmbeddingProvider(1024), CHUNKS)
        assert written == 3

        found = await search_laws(
            store, MockEmbeddingProvider(1024), "проценты за пользование", top_k=3
        )
        assert found
        assert found[0].embedding.document_type == "LAW"

    async def test_empty_chunks_nothing_written(self) -> None:
        store = VectorStore(QdrantClient(":memory:"), dimension=1024)
        assert await index_law_chunks(store, MockEmbeddingProvider(1024), []) == 0

    async def test_empty_query_returns_nothing(self) -> None:
        store = VectorStore(QdrantClient(":memory:"), dimension=1024)
        assert await search_laws(store, MockEmbeddingProvider(1024), "  ") == []


class TestContext:
    async def test_context_has_citation(self) -> None:
        store = VectorStore(QdrantClient(":memory:"), dimension=1024)
        await index_law_chunks(store, MockEmbeddingProvider(1024), CHUNKS)
        results = await search_laws(
            store, MockEmbeddingProvider(1024), "обязательства должны исполняться", top_k=1
        )
        blocks = format_law_context(results, max_chars=2000)
        assert blocks
        assert blocks[0].startswith(("ГК РФ", "ГПК РФ"))
        assert "Статья" in blocks[0]


class TestLawContextInPrompt:
    def test_norms_added_to_reasoning_sections(self) -> None:
        from app.agents.draft_agent import build_section_prompt
        from app.agents.evidence_pack import EvidencePack, load_template

        pack = EvidencePack(
            case_number="2-1/2026",
            court_name="Суд",
            jurisdiction="civil",
            law_context=["ГК РФ, Статья 395. Ответственность: проценты."],
        )
        template = load_template()
        sections = {item["key"]: item for item in template["sections"]}

        motivating, _ = build_section_prompt(sections["motivating"], pack)
        established, _ = build_section_prompt(sections["established"], pack)
        operative, _ = build_section_prompt(sections["operative"], pack)

        assert "нормы права из базы знаний" in motivating
        assert "нормы права из базы знаний" in established
        assert "нормы права из базы знаний" not in operative


class TestLocalPersistence:
    async def test_index_survives_reopen(self, tmp_path: Path) -> None:
        """Файловый режим: индекс не исчезает после перезапуска.

        Ради этого режим и добавлялся: в `memory` база знаний строилась
        заново после каждого рестарта службы.
        """
        settings = Settings(
            qdrant_mode=QdrantMode.LOCAL,
            qdrant_path=str(tmp_path / "qdrant"),
            embedding_dim=1024,
        )
        store = VectorStore.from_settings(settings)
        assert await index_law_chunks(store, MockEmbeddingProvider(1024), CHUNKS) == 3
        store.client.close()

        reopened = VectorStore.from_settings(settings)
        results = await search_laws(
            reopened, MockEmbeddingProvider(1024), "проценты", top_k=3
        )
        reopened.client.close()

        assert len(results) == 3
        assert CollectionName.LAWS.value == "legal_laws"
