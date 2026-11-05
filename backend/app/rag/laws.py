"""База знаний по нормам: индексация кодексов и поиск статей.

Зачем: на живом тесте 21 из 26 проектов применяли не тот кодекс, а
выдуманные ссылки на нормы встречались регулярно. Модель ссылается
увереннее, когда рядом лежит текст нормы, а не только её номер.

Модуль сознательно не решает, какие нормы применимы: он возвращает
наиболее близкие пассажи с точной ссылкой, а выбор остаётся за моделью
и проверкой. Ошибка поиска дешевле, чем подмена решения суда.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.config import Settings, get_settings
from app.datasets.law_corpus import LawChunk
from app.embeddings import build_embedding_provider
from app.embeddings.base import Embedding, EmbeddingProvider
from app.rag.store import CollectionName, SearchResult, VectorStore

logger = logging.getLogger(__name__)

#: Тип документа для пассажа закона: отличается от материалов дела.
LAW_DOCUMENT_TYPE = "LAW"


def _to_embedding(chunk: LawChunk, vector: Sequence[float]) -> Embedding:
    return Embedding(
        vector=list(vector),
        chunk_id=chunk.chunk_id,
        document_id=chunk.code,
        # Ссылка, которую увидит человек: «ГК РФ, Статья 395. …».
        filename=f"Статья {chunk.article}. {chunk.title}",
        document_type=LAW_DOCUMENT_TYPE,
        paragraph=chunk.order,
        text=chunk.text,
    )


async def index_law_chunks(
    store: VectorStore,
    embedder: EmbeddingProvider,
    chunks: Sequence[LawChunk],
    *,
    batch_size: int = 32,
) -> int:
    """Записать пассажи кодекса в коллекцию норм.

    Возвращает число записанных пассажей. Индексация идемпотентна:
    chunk_id детерминирован, повторный запуск обновляет те же точки.
    """
    written = 0
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = await embedder.embed([chunk.text for chunk in batch])
        if not vectors:
            logger.warning("Эмбеддинги недоступны: индексация норм остановлена")
            break
        embeddings = [
            _to_embedding(chunk, vector)
            for chunk, vector in zip(batch, vectors, strict=True)
        ]
        written += store.upsert(CollectionName.LAWS.value, embeddings)
    return written


async def search_laws(
    store: VectorStore,
    embedder: EmbeddingProvider,
    query: str,
    *,
    top_k: int = 5,
) -> list[SearchResult]:
    """Найти ближайшие статьи кодекса по тексту запроса."""
    if not query.strip():
        return []
    vectors = await embedder.embed([query])
    if not vectors:
        return []
    return store.search(CollectionName.LAWS.value, vectors[0], limit=top_k)


def cite(result: SearchResult) -> str:
    """Точная ссылка на норму: «ГК РФ, Статья 395. …»."""
    embedding = result.embedding
    if embedding.filename:
        return f"{embedding.document_id}, {embedding.filename}"
    return embedding.document_id


def format_law_context(
    results: Sequence[SearchResult], *, max_chars: int = 1500
) -> list[str]:
    """Собрать блок норм для подсказки модели в пределах бюджета.

    Текст нормы важнее количества: пять обрывков без определения хуже,
    чем две статьи целиком.
    """
    blocks: list[str] = []
    used = 0
    seen: set[str] = set()

    for result in results:
        text = (result.embedding.text or "").strip()
        if not text:
            continue
        header = cite(result)
        if header in seen:
            continue
        seen.add(header)
        block = f"{header}:\n{text}"
        if blocks and used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
    return blocks


async def law_context_for_case(
    query: str, settings: Settings | None = None
) -> list[str]:
    """Нормы по запросу дела — или пусто, если база знаний выключена.

    Любой сбой (нет индекса, нет эмбеддингов) не должен ломать
    генерацию: проект формируется без блока норм, как раньше.
    """
    cfg = settings or get_settings()
    if not cfg.rag_laws_enabled or not (query or "").strip():
        return []
    try:
        store = VectorStore.from_settings(cfg)
        embedder = build_embedding_provider(cfg)
        results = await search_laws(
            store, embedder, query[:2000], top_k=cfg.rag_laws_top_k
        )
        return format_law_context(results, max_chars=cfg.rag_laws_context_chars)
    except Exception as exc:  # noqa: BLE001
        logger.warning("База знаний по нормам недоступна: %s", exc)
        return []
