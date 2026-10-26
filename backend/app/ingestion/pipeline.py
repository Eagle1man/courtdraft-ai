"""Пайплайн ingestion (Фаза 5, п.13).

    Upload -> Validation -> SHA256 -> Text extraction -> Classification
           -> Cleaning -> Structural chunking -> Embedding -> Qdrant
           -> Metadata -> PostgreSQL

Каждый шаг проваливается явно, а не молча: пропущенный или пустой
фрагмент в проекте хуже явной ошибки, потому что выглядит как
«документ не содержит этого факта», а не «мы его не обработали».
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repositories import AuditRepository, DocumentRepository
from app.embeddings import get_embedding_provider
from app.embeddings.base import Embedding, EmbeddingProvider
from app.ingestion.chunking import Chunk, chunk_text
from app.ingestion.extract import (
    ExtractionError,
    extract_text,
    guess_document_type,
)
from app.models.entities import CaseDocument, DocumentChunk
from app.rag.store import CollectionName, VectorStore

logger = logging.getLogger(__name__)


class IngestionError(ValueError):
    """Ошибка ingestion. Сообщение показывается пользователю."""


@dataclass
class IngestionResult:
    """Результат обработки одного документа."""

    document_id: uuid.UUID
    filename: str
    sha256: str
    document_type: str
    page_count: int
    character_count: int
    chunk_count: int
    indexed_count: int
    embedding_status: str
    # Что именно не удалось — для честного отчёта перед пользователем.
    warnings: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    took_ms: int = 0


async def ingest_document(
    session: AsyncSession,
    case_id: uuid.UUID,
    filename: str,
    data: bytes,
    *,
    document_type: str = "",
    mime_type: str = "",
    store: VectorStore | None = None,
    embeddings: EmbeddingProvider | None = None,
    settings: Settings | None = None,
    actor: str = "api",
) -> IngestionResult:
    """Обработать один документ: от файла до индекса."""
    cfg = settings or get_settings()
    embedding_provider = embeddings or get_embedding_provider()
    audit = AuditRepository(session)
    started = datetime.now(UTC)

    # ── 1. Валидация размера ─────────────────────────────────────────
    if not data:
        raise IngestionError(f"{filename}: файл пуст")

    if len(data) > cfg.max_upload_bytes:
        raise IngestionError(
            f"{filename}: размер {len(data) // 1024 // 1024} МБ превышает "
            f"лимит {cfg.max_upload_mb} МБ (MAX_UPLOAD_MB в .env)"
        )

    # ── 2. SHA256 ───────────────────────────────────────────────────
    sha256 = hashlib.sha256(data).hexdigest()
    documents = DocumentRepository(session)

    # Дубль ищем внутри дела, а не по всей базе: один и тот же документ
    # законно встречается в разных делах (типовой иск, типовой договор),
    # глобальная проверка запрещала бы вторую загрузку туда же.
    existing = await documents.find_by_sha256(sha256, case_id=case_id)
    if existing is not None:
        raise IngestionError(
            f"{filename}: документ с таким содержимым уже загружен "
            f"({existing.filename}, SHA256 совпадает)"
        )

    warnings: list[str] = []

    # ── 3. Извлечение текста ─────────────────────────────────────────
    try:
        extracted = extract_text(filename, data, mime_type=mime_type, settings=cfg)
    except ExtractionError as exc:
        # Документ не читается — фиксируем в аудите и сообщаем.
        # Не создаём «пустой» документ: он выглядел бы как законченный.
        await audit.log(
            "document.extraction_failed",
            case_id=case_id,
            actor=actor,
            filename=filename,
            error=str(exc)[:200],
        )
        raise IngestionError(str(exc)) from exc

    # ── 4. Классификация ─────────────────────────────────────────────
    resolved_type = document_type or guess_document_type(filename, extracted.text)

    # ── 5. Сохранение документа ──────────────────────────────────────
    document = await documents.add(
        CaseDocument(
            case_id=case_id,
            filename=filename,
            document_type=resolved_type,
            mime_type=mime_type or str(extracted.metadata.get("detected_mime_type", "")),
            page_count=extracted.page_count,
            text=extracted.text,
            metadata_json={**extracted.metadata, "ingested_at": started.isoformat()},
            sha256=sha256,
        )
    )

    # ── 6. Структурный чанкинг ───────────────────────────────────────
    chunks = chunk_text(
        extracted.text,
        document_id=str(document.id),
        pages=extracted.pages,
        settings=cfg,
    )

    if not chunks:
        warnings.append(
            f"{filename}: текст извлечён, но структурная разбивка дала "
            f"0 фрагментов — проверьте разметку документа"
        )

    chunk_rows = [
        DocumentChunk(
            document_id=document.id,
            case_id=case_id,
            chunk_index=chunk.index,
            text=chunk.text,
            page=chunk.page,
            paragraph=chunk.paragraph,
            character_start=chunk.character_start,
            character_end=chunk.character_end,
            anchor=chunk.anchor,
        )
        for chunk in chunks
    ]
    if chunk_rows:
        await documents.replace_chunks(document.id, chunk_rows)

    # ── 7. Эмбеддинги и индекс ───────────────────────────────────────
    indexed = 0
    if chunks:
        indexed = await _index_chunks(
            document,
            chunks,
            resolved_type,
            store=store,
            embeddings=embedding_provider,
            settings=cfg,
            warnings=warnings,
        )

    await audit.log(
        "document.ingested",
        case_id=case_id,
        actor=actor,
        document_id=str(document.id),
        filename=filename,
        document_type=resolved_type,
        characters=len(extracted.text),
        chunks=len(chunks),
        indexed=indexed,
        embedding_status=embedding_provider.status.value,
        warnings=len(warnings),
    )

    return IngestionResult(
        document_id=document.id,
        filename=filename,
        sha256=sha256,
        document_type=resolved_type,
        page_count=extracted.page_count,
        character_count=len(extracted.text),
        chunk_count=len(chunks),
        indexed_count=indexed,
        embedding_status=embedding_provider.status.value,
        warnings=warnings,
        chunks=chunks,
        took_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
    )


async def _index_chunks(
    document: CaseDocument,
    chunks: list[Chunk],
    document_type: str,
    *,
    store: VectorStore | None,
    embeddings: EmbeddingProvider,
    settings: Settings,
    warnings: list[str],
) -> int:
    """Построить векторы и записать их в индекс.

    Отказ индексации не отменяет ingestion: документ, чанки и текст уже
    сохранены, а поиск по нему можно будет построить позже. Но об этом
    обязательно сообщаем в warnings — иначе пользователь посчитает, что
    документ проиндексирован.
    """
    # Нормативная база и практика индексируются отдельно (Фаза 4 коллекции
    # legal_laws / legal_cases). Здесь только документы дела.
    collection = CollectionName.DOCUMENTS.value

    texts = [chunk.text for chunk in chunks]

    try:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), settings.embedding_batch_size):
            batch = texts[start : start + settings.embedding_batch_size]
            vectors.extend(await embeddings.embed(batch))
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"Эмбеддинги не построены ({type(exc).__name__}): {str(exc)[:120]}. "
            f"Документ сохранён и доступен для чтения, поиск по нему "
            f"пока не работает."
        )
        logger.warning("Не удалось построить эмбеддинги: %s", exc)
        return 0

    if not vectors:
        warnings.append("Провайдер эмбеддингов вернул пустой результат")
        return 0

    if len(vectors) != len(chunks):
        warnings.append(
            f"Провайдер эмбеддингов вернул {len(vectors)} векторов "
            f"вместо {len(chunks)} — часть фрагментов не проиндексирована"
        )

    embeddings_out = [
        Embedding(
            vector=vectors[i],
            chunk_id=chunk.anchor,
            document_id=str(document.id),
            filename=document.filename,
            document_type=document_type,
            page=chunk.page,
            paragraph=chunk.paragraph,
            character_start=chunk.character_start,
            character_end=chunk.character_end,
            text=chunk.text[:4000],
        )
        for i, chunk in enumerate(chunks[: len(vectors)])
    ]

    vector_store = store or VectorStore.from_settings(settings)
    written = vector_store.upsert(collection, embeddings_out)

    if written == 0:
        warnings.append(
            f"Индекс {collection} недоступен — фрагменты сохранены в БД, "
            f"но векторный поиск по ним не работает"
        )

    return written


async def reindex_case(
    session: AsyncSession,
    case_id: uuid.UUID,
    *,
    store: VectorStore | None = None,
    embeddings: EmbeddingProvider | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Перестроить индекс по всем документам дела.

    Нужна после смены параметров чанкинга: CHUNK_SIZE из `.env` влияет
    на всю индексацию, и без пересборки старые фрагменты останутся
    со старыми границами.
    """
    cfg = settings or get_settings()
    documents = DocumentRepository(session)
    rows = await documents.list_for_case(case_id)

    vector_store = store or VectorStore.from_settings(cfg)
    provider = embeddings or get_embedding_provider()

    # Старое удаляем целиком: иначе останутся векторы от прежних
    # фрагментов, и поиск будет возвращать удалённое содержимое.
    vector_store.delete_by_case(CollectionName.DOCUMENTS.value, str(case_id))

    total_chunks = 0
    total_indexed = 0

    for document in rows:
        chunks = chunk_text(
            document.text,
            document_id=str(document.id),
            pages=[],  # страницы не восстановлены из БД намеренно
            settings=cfg,
        )
        if not chunks:
            continue

        await documents.replace_chunks(
            document.id,
            [
                DocumentChunk(
                    document_id=document.id,
                    case_id=case_id,
                    chunk_index=c.index,
                    text=c.text,
                    page=c.page,
                    paragraph=c.paragraph,
                    character_start=c.character_start,
                    character_end=c.character_end,
                    anchor=c.anchor,
                )
                for c in chunks
            ],
        )
        warnings: list[str] = []
        indexed = await _index_chunks(
            document,
            chunks,
            document.document_type,
            store=vector_store,
            embeddings=provider,
            settings=cfg,
            warnings=warnings,
        )
        total_chunks += len(chunks)
        total_indexed += indexed
        for warning in warnings:
            logger.warning("reindex %s: %s", document.filename, warning)

    return {"documents": len(rows), "chunks": total_chunks, "indexed": total_indexed}
