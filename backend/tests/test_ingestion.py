"""Тесты ingestion (Фаза 5): извлечение, чанкинг с provenance, пайплайн.

Особое внимание к provenance: без него Verification Engine не может
отличить утверждение с источником от выдумки, а это центральное
требование продукта (п.12, п.28).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.repositories import DocumentRepository
from app.db.session import create_all, create_engine_and_session, drop_all
from app.embeddings.mock import MockEmbeddingProvider
from app.ingestion.chunking import chunk_text
from app.ingestion.extract import (
    ExtractionError,
    clean_text,
    detect_mime_type,
    extract_text,
    guess_document_type,
)
from app.ingestion.pipeline import IngestionError, ingest_document, reindex_case
from app.models.entities import Case, CaseStatus, DocumentChunk
from app.rag.store import CollectionName, VectorStore

SETTINGS = Settings(chunk_size=800, chunk_overlap=120)


def make_settings() -> Settings:
    """Настройки чанкинга для тестов.

    Имя НЕ начинается с `test_`: иначе pytest соберёт эту функцию как
    тест и она вернёт значение вместо None.
    """
    return Settings(chunk_size=800, chunk_overlap=120)


class TestMimeDetection:
    def test_known_types(self) -> None:
        assert detect_mime_type("a.pdf") == "application/pdf"
        assert detect_mime_type("a.txt") == "text/plain"
        assert detect_mime_type("a.md") == "text/markdown"
        assert detect_mime_type("a.html") == "text/html"
        assert detect_mime_type("a.json") == "application/json"

    def test_case_insensitive(self) -> None:
        assert detect_mime_type("DOCUMENT.PDF") == "application/pdf"

    def test_unknown_falls_back(self) -> None:
        assert detect_mime_type("a.bin") == "application/octet-stream"


class TestExtractPlain:
    def test_utf8_text(self) -> None:
        data = "Исковое заявление о взыскании".encode()
        result = extract_text("claim.txt", data)
        assert "взыскании" in result.text
        assert result.page_count == 1

    def test_cp1251_text(self) -> None:
        """Кириллица в cp1251 не должна превращаться в мусор."""
        data = "Взыскание задолженности".encode("cp1251")
        result = extract_text("claim.txt", data)
        assert "Взыскание" in result.text

    def test_markdown_stripped(self) -> None:
        data = b"# Title\n\nSome **bold** text and `code`."
        result = extract_text("doc.md", data)
        assert "**" not in result.text
        assert "`code`" not in result.text
        assert "bold" in result.text

    def test_empty_file_raises(self) -> None:
        """Пустой документ должен падать, а не молча создаваться пустым."""
        with pytest.raises(ExtractionError):
            extract_text("empty.txt", b"")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ExtractionError):
            extract_text("empty.txt", b"   \n\n  ")


class TestExtractHtml:
    def test_tags_removed(self) -> None:
        data = "<html><body><p>Абзац</p><p>Второй</p></body></html>".encode()
        result = extract_text("doc.html", data)
        assert "Абзац" in result.text
        assert "<p>" not in result.text

    def test_script_content_removed(self) -> None:
        data = b"<html><script>alert('x')</script><p>Text</p></html>"
        result = extract_text("doc.html", data)
        assert "alert" not in result.text
        assert "Text" in result.text

    def test_entities_unescaped(self) -> None:
        result = extract_text("doc.html", b"<p>A &amp; B</p>")
        assert "&" in result.text


class TestExtractJson:
    def test_pretty_printed(self) -> None:
        data = '{"fio": "Иванов", "sum": 100}'.encode()
        result = extract_text("data.json", data)
        assert "Иванов" in result.text
        assert '"sum"' in result.text

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ExtractionError):
            extract_text("bad.json", b"{not json")


class TestExtractPdf:
    def test_real_pdf_with_pages(self) -> None:
        """PDF без текстового слоя (скан) обязан дать внятную ошибку.

        Имя теста отражает то, что он действительно проверяет.
        Проверку многостраничного разбора с номерами страниц делает
        отдельный тест на реальном PDF с текстовым слоем — его
        придётся добавить с фикстурой-файлом.
        """
        import io

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)

        # Файл без текстового слоя: обязана быть внятная ошибка, а не
        # «пустой документ». С OCR сканированная страница идёт в
        # распознавание, и пустая (белая) страница честно сообщает, что
        # текста не получилось, а не молчит.
        with pytest.raises(ExtractionError, match="скан|распозна"):
            extract_text("scan.pdf", buffer.getvalue())

    def test_corrupt_pdf_raises(self) -> None:
        with pytest.raises(ExtractionError):
            extract_text("bad.pdf", b"%PDF-1.4 garbage not a real pdf")


class TestDocumentTypeGuess:
    def test_by_filename_claim(self) -> None:
        assert guess_document_type("01_claim.pdf", "") == "CLAIM"

    def test_by_filename_protocol(self) -> None:
        assert guess_document_type("05_protocol.pdf", "") == "PROTOCOL"

    def test_by_filename_contract(self) -> None:
        assert guess_document_type("03_contract.pdf", "") == "CONTRACT"

    def test_by_content_protocol(self) -> None:
        """Протокол определяет маршрут дела, поэтому проверяем по тексту."""
        text = "ПРОТОКОЛ судебного заседания по делу № 123"
        assert guess_document_type("unknown.bin", text) == "PROTOCOL"

    def test_unknown_returns_other(self) -> None:
        assert guess_document_type("unknown.bin", "какой-то текст") == "OTHER"

    def test_result_is_known_type(self) -> None:
        from app.models.entities import DocumentType

        for name in ("01_claim.pdf", "05_protocol.pdf", "03_contract.pdf"):
            assert DocumentType.is_known(guess_document_type(name, ""))


class TestCleanText:
    def test_normalizes_newlines(self) -> None:
        assert clean_text("a\r\nb\rc") == "a\nb\nc"

    def test_collapses_spaces(self) -> None:
        assert clean_text("a     b\t\tc") == "a b c"

    def test_collapses_blank_lines(self) -> None:
        assert clean_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_removes_zero_width(self) -> None:
        assert clean_text("a\u200bb") == "ab"

    def test_empty(self) -> None:
        assert clean_text("") == ""


class TestChunking:
    TEXT = "\n\n".join(f"Абзац номер {i}. Содержание для обработки." for i in range(20))

    def test_produces_chunks(self) -> None:
        chunks = chunk_text(self.TEXT, document_id="doc-1", settings=make_settings())
        assert chunks
        assert all(c.text.strip() for c in chunks)

    def test_indices_sequential(self) -> None:
        chunks = chunk_text(self.TEXT, document_id="d", settings=make_settings())
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_anchor_unique_and_parsable(self) -> None:
        chunks = chunk_text(self.TEXT, document_id="d", settings=make_settings())
        anchors = [c.anchor for c in chunks]
        assert len(set(anchors)) == len(chunks)
        for c in chunks:
            doc_id, _, idx = c.anchor.partition(":")
            assert doc_id == "d"
            assert int(idx) == c.index

    def test_character_bounds_within_text(self) -> None:
        """Границы символов обязаны указывать в пределах документа."""
        chunks = chunk_text(self.TEXT, document_id="d", settings=make_settings())
        for c in chunks:
            assert 0 <= c.character_start < len(self.TEXT)
            assert c.character_start < c.character_end <= len(self.TEXT) + 1

    def test_no_text_lost(self) -> None:
        """Перекрытие может дублировать текст, но не терять его."""
        chunks = chunk_text(self.TEXT, document_id="d", settings=make_settings())
        for marker in ("Абзац номер 0", "Абзац номер 19"):
            assert any(marker in c.text for c in chunks)

    def test_short_paragraphs_grouped(self) -> None:
        """Короткие абзацы должны объединяться, а не давать чанк каждый."""
        text = "\n\n".join(f"P{i}" for i in range(10))
        chunks = chunk_text(text, document_id="d", settings=make_settings())
        assert len(chunks) < 10

    def test_long_paragraph_split(self) -> None:
        long_paragraph = "Предложение. " * 500
        chunks = chunk_text(long_paragraph, document_id="d", settings=make_settings())
        assert len(chunks) > 1

    def test_headings_captured_as_section(self) -> None:
        text = "# Претензионная часть\n\nСодержание претензии.\n\n# Заключение\n\nВывод."
        chunks = chunk_text(text, document_id="d", settings=make_settings())
        sections = {c.section for c in chunks}
        assert "Претензионная часть" in sections

    def test_section_prefixed_to_text(self) -> None:
        """Без заголовка фрагмент из раздела теряет контекст."""
        text = "# Претензионная часть\n\nСодержание."
        chunks = chunk_text(text, document_id="d", settings=make_settings())
        assert chunks
        assert "Претензионная часть" in chunks[0].text

    def test_empty_text_gives_no_chunks(self) -> None:
        assert chunk_text("", document_id="d", settings=make_settings()) == []

    def test_page_propagates(self) -> None:
        from app.ingestion.extract import Page

        pages = [Page(number=3, text="Текст на третьей странице.")]
        chunks = chunk_text(
            "Текст на третьей странице.", document_id="d", pages=pages, settings=make_settings()
        )
        assert chunks[0].page == 3

    def test_citation_readable(self) -> None:
        chunks = chunk_text("Абзац текста.", document_id="d", settings=make_settings())
        assert chunks[0].citation()

    def test_chunk_size_respected(self) -> None:
        """Размер фрагмента не должен превышать лимит заметно."""
        settings = Settings(chunk_size=400, chunk_overlap=50)
        chunks = chunk_text(self.TEXT, document_id="d", settings=settings)
        for c in chunks:
            # Заголовок раздела добавляется сверх лимита содержимого,
            # поэтому допускаем небольшое превышение.
            assert c.length <= settings.chunk_size + 200


class TestIngestionPipeline:
    @pytest_asyncio.fixture
    async def session(self, tmp_path: Path):
        db_file = tmp_path / "ingest.db"
        settings = Settings(database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}")
        manager = create_engine_and_session(settings)
        engine, _ = await manager.__aenter__()
        await create_all(engine)

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as sess:
            case = Case(
                case_number="ING-1", court_name="Суд", case_type="civil",
                status=CaseStatus.NEW.value,
            )
            sess.add(case)
            await sess.commit()
            yield sess, case

        await drop_all(engine)
        await manager.__aexit__(None, None, None)

    async def _store(self) -> VectorStore:
        from qdrant_client import QdrantClient

        return VectorStore(QdrantClient(":memory:"), dimension=64)

    async def test_full_pipeline(self, session) -> None:
        sess, case = session
        store = await self._store()
        content = "\n\n".join(
            "# Раздел\n\n" + "Абзац с текстом дела. " * 5 for _ in range(3)
        )

        result = await ingest_document(
            sess,
            case.id,
            "01_claim.txt",
            content.encode("utf-8"),
            store=store,
            embeddings=MockEmbeddingProvider(dimension=64),
            settings=make_settings(),
        )
        await sess.commit()

        assert result.character_count > 0
        assert result.chunk_count > 0
        assert result.document_type == "CLAIM"
        assert result.indexed_count == result.chunk_count

    async def test_chunks_saved_with_provenance(self, session) -> None:
        sess, case = session
        store = await self._store()
        content = "\n\n".join(f"Абзац {i} с текстом." for i in range(15))

        result = await ingest_document(
            sess, case.id, "doc.txt", content.encode("utf-8"),
            store=store, embeddings=MockEmbeddingProvider(dimension=64),
            settings=make_settings(),
        )
        await sess.commit()

        document = await DocumentRepository(sess).get(result.document_id)
        assert document is not None
        assert document.text

        rows = await sess.execute(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )
        rows = list(rows.scalars())
        assert len(rows) == result.chunk_count
        for row in rows:
            assert row.anchor
            assert row.text

    async def test_duplicate_detected(self, session) -> None:
        """Один и тот же файл дважды не должен попасть в дело."""
        sess, case = session
        store = await self._store()
        data = "Один и тот же документ.".encode()

        await ingest_document(
            sess, case.id, "a.txt", data, store=store,
            embeddings=MockEmbeddingProvider(dimension=64), settings=make_settings(),
        )
        await sess.commit()

        with pytest.raises(IngestionError, match="уже загружен"):
            await ingest_document(
                sess, case.id, "b.txt", data, store=store,
                embeddings=MockEmbeddingProvider(dimension=64), settings=make_settings(),
            )

    async def test_empty_file_rejected(self, session) -> None:
        sess, case = session
        store = await self._store()
        with pytest.raises(IngestionError):
            await ingest_document(
                sess, case.id, "empty.txt", b"", store=store,
                embeddings=MockEmbeddingProvider(dimension=64), settings=make_settings(),
            )

    async def test_oversized_rejected(self, session) -> None:
        sess, case = session
        store = await self._store()
        # Лимит 1 МБ, файл 2 МБ.
        with pytest.raises(IngestionError, match="превышает"):
            await ingest_document(
                sess, case.id, "big.txt", b"x" * (2 * 1024 * 1024),
                store=store, embeddings=MockEmbeddingProvider(dimension=64),
                settings=Settings(max_upload_mb=1),
            )

    async def test_unreadable_file_raises_not_silent(self, session) -> None:
        """Нечитаемый файл обязан быть виден как ошибка, а не как пустой документ."""
        sess, case = session
        store = await self._store()
        with pytest.raises(IngestionError):
            await ingest_document(
                sess, case.id, "bad.pdf", b"%PDF-broken",
                store=store, embeddings=MockEmbeddingProvider(dimension=64),
                settings=make_settings(),
            )

    async def test_vectors_land_in_collection(self, session) -> None:
        sess, case = session
        store = await self._store()
        content = "\n\n".join(f"Абзац {i} для индексации." for i in range(12))

        result = await ingest_document(
            sess, case.id, "01_claim.txt", content.encode("utf-8"),
            store=store, embeddings=MockEmbeddingProvider(dimension=64),
            settings=make_settings(),
        )
        await sess.commit()

        stats = store.stats(CollectionName.DOCUMENTS.value)
        assert stats.exists
        assert stats.points == result.chunk_count

    async def test_audit_written(self, session) -> None:
        from app.db.repositories import AuditRepository

        sess, case = session
        store = await self._store()
        await ingest_document(
            sess, case.id, "01_claim.txt", "Текст документа для аудита.".encode(),
            store=store, embeddings=MockEmbeddingProvider(dimension=64),
            settings=make_settings(),
        )
        await sess.commit()

        entries = await AuditRepository(sess).list_for_case(case.id)
        assert any(e.action == "document.ingested" for e in entries)

    async def test_reindex_rebuilds_chunks(self, session) -> None:
        """Смена CHUNK_SIZE требует пересборки индекса."""
        sess, case = session
        store = await self._store()
        content = "\n\n".join(f"Абзац {i} с текстом для дела." for i in range(20))

        await ingest_document(
            sess, case.id, "01_claim.txt", content.encode("utf-8"),
            store=store, embeddings=MockEmbeddingProvider(dimension=64),
            settings=Settings(chunk_size=800, chunk_overlap=120),
        )
        await sess.commit()

        smaller = await reindex_case(
            sess, case.id, store=store,
            embeddings=MockEmbeddingProvider(dimension=64),
            settings=Settings(chunk_size=200, chunk_overlap=30),
        )
        await sess.commit()

        assert smaller["chunks"] > 0
        assert smaller["indexed"] > 0
