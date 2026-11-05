"""Векторное хранилище (Фаза 4, п.25).

Qdrant работает в двух режимах:

* `QDRANT_MODE=server` — реальный сервер Qdrant (RTX 3050);
* `QDRANT_MODE=memory` — встроенный режим `QdrantClient(":memory:")`.

Встроенный режим нужен потому, что на машине разработки сервер Qdrant
не поднять: Docker недоступен, а установка требует отдельного процесса.
Он поддерживает всё, что нужно для RAG: векторы, payload-фильтры,
поиск по сходству. Разница с сервером — только в отказоустойчивости
и в том, что данные не переживают перезапуск.

Три коллекции из п.25: legal_laws, legal_cases, case_documents.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from qdrant_client import QdrantClient, models

from app.config import PROJECT_ROOT, QdrantMode, Settings, get_settings
from app.embeddings.base import Embedding

logger = logging.getLogger(__name__)


class CollectionName(StrEnum):
    """Коллекции (п.25)."""

    LAWS = "legal_laws"
    CASES = "legal_cases"
    DOCUMENTS = "case_documents"

    @classmethod
    def all(cls) -> list[str]:
        return [c.value for c in cls]


@dataclass(frozen=True)
class SearchFilters:
    """Фильтры поиска (п.25: metadata filtering).

    Каждое поле соответствует реальному ограничению бизнес-логики:
    искать нормы только в действительной редакции и только своего суда —
    не «удобство», а требование юридической корректности.
    """

    case_id: str | None = None
    document_id: str | None = None
    document_type: str | None = None
    court: str | None = None
    category: str | None = None
    # Дата, на которую нужна редакция нормы (п.16). Тип date, а не str:
    # Qdrant сравнивает даты как даты, и строковое сравнение ISO-строк
    # совпало бы по порядку только случайно.
    effective_on: date | None = None


@dataclass
class SearchResult:
    """Результат поиска с provenance."""

    embedding: Embedding
    score: float
    collection: str

    @property
    def chunk_id(self) -> str:
        return self.embedding.chunk_id


@dataclass
class IndexStats:
    """Состояние индекса для /api/health и UI."""

    collection: str
    points: int = 0
    exists: bool = False
    error: str = ""


def _point_id(chunk_id: str) -> str:
    """Привести id к формату UUID, который требует Qdrant.

    Qdrant принимает только UUID или целое число. Наши chunk_id —
    произвольные строки, поэтому из них детерминированно выводится UUID:
    один и тот же chunk_id всегда даёт один и тот же UUID, что нужно
    для идемпотентной переиндексации.
    """
    try:
        return str(uuid.UUID(chunk_id))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


class VectorStore:
    """Обёртка над Qdrant.

    Содержит только работу с индексом: создание коллекций, upsert,
    поиск. Семантика (эмбеддинги, RRF, фильтры по смыслу) — в слое RAG.
    """

    def __init__(self, client: QdrantClient, *, dimension: int) -> None:
        self._client = client
        self._dimension = dimension
        self._ready_collections: set[str] = set()

    # ── Создание ─────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> VectorStore:
        """Создать хранилище по настройкам."""
        cfg = settings or get_settings()

        if cfg.qdrant_mode is QdrantMode.SERVER:
            client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None)
        elif cfg.qdrant_mode is QdrantMode.LOCAL:
            # Файловый режим: индекс переживает перезапуск службы и не
            # требует отдельного сервера. Без него база знаний по нормам
            # исчезала бы после каждого рестарта — строить её заново на
            # 2 543 пассажа долго.
            path = Path(cfg.qdrant_path)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            path.mkdir(parents=True, exist_ok=True)
            client = QdrantClient(path=str(path))
        else:
            # Встроенный режим: тестовый и MOCK-конвейер без сервера.
            client = QdrantClient(":memory:")

        return cls(client, dimension=cfg.embedding_dim)

    @property
    def client(self) -> QdrantClient:
        return self._client

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_embedded(self) -> bool:
        """Работает ли встроенный режим (без сервера).

        Определяется по наличию локального клиента внутри обёртки:
        у QdrantClient поле называется `_client`, и в серверном режиме
        там REST-клиент, а во встроенном — QdrantLocal.
        """
        inner = getattr(self._client, "_client", None)
        return inner is not None and type(inner).__name__ == "QdrantLocal"

    def ensure_collection(self, name: str) -> bool:
        """Создать коллекцию, если её нет.

        Идемпотентно: повторный вызов ничего не делает. Это позволяет
        вызыватьensure на каждом старте, не следя за состоянием индекса.
        """
        if name in self._ready_collections:
            return True

        try:
            if not self._client.collection_exists(name):
                self._client.create_collection(
                    collection_name=name,
                    vectors_config=models.VectorParams(
                        size=self._dimension,
                        distance=models.Distance.COSINE,
                    ),
                )
                logger.info("Создана коллекция %s (dim=%d)", name, self._dimension)
            self._ready_collections.add(name)
            return True
        except Exception as exc:  # noqa: BLE001
            # Индекс не критичен для старта: retrieval деградирует,
            # система продолжает работать (подход п.53).
            logger.warning("Не удалось создать коллекцию %s: %s", name, exc)
            return False

    def ensure_all(self) -> dict[str, bool]:
        return {name: self.ensure_collection(name) for name in CollectionName.all()}

    # ── Запись ───────────────────────────────────────────────────────

    def upsert(self, collection: str, embeddings: Sequence[Embedding]) -> int:
        """Записать или обновить векторы.

        Idempotентно по chunk_id: повторная индексация того же фрагмента
        не создаёт дублей.
        """
        if not embeddings:
            return 0

        if not self.ensure_collection(collection):
            return 0

        points = [
            models.PointStruct(
                id=_point_id(emb.chunk_id),
                vector=emb.vector,
                payload={
                    "chunk_id": emb.chunk_id,
                    "document_id": emb.document_id,
                    "case_id": _extract_case_id(emb.chunk_id),
                    "filename": emb.filename,
                    "document_type": emb.document_type,
                    "page": emb.page,
                    "paragraph": emb.paragraph,
                    "character_start": emb.character_start,
                    "character_end": emb.character_end,
                    "text": emb.text[:4000],
                },
            )
            for emb in embeddings
        ]

        try:
            self._client.upsert(collection_name=collection, points=points)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось записать в %s: %s", collection, exc)
            return 0
        return len(points)

    def delete_by_case(self, collection: str, case_id: str) -> None:
        """Удалить все векторы дела (при переиндексации)."""
        try:
            self._client.delete(
                collection_name=collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(must=[_match("case_id", case_id)])
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось удалить векторы дела %s: %s", case_id, exc)

    # ── Поиск ────────────────────────────────────────────────────────

    def search(
        self,
        collection: str,
        vector: Sequence[float],
        *,
        limit: int = 10,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        """Поиск по сходству с фильтрами по метаданным."""
        query_filter = build_filter(filters)

        try:
            response = self._client.query_points(
                collection_name=collection,
                query=list(vector),
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Поиск в %s не удался: %s", collection, exc)
            return []

        results: list[SearchResult] = []
        for point in response.points:
            payload = point.payload or {}
            results.append(
                SearchResult(
                    embedding=Embedding(
                        vector=[],
                        chunk_id=str(payload.get("chunk_id", "")),
                        document_id=str(payload.get("document_id", "")),
                        filename=str(payload.get("filename", "")),
                        document_type=str(payload.get("document_type", "")),
                        page=payload.get("page"),
                        paragraph=payload.get("paragraph"),
                        character_start=payload.get("character_start"),
                        character_end=payload.get("character_end"),
                        text=str(payload.get("text", "")),
                    ),
                    score=float(point.score),
                    collection=collection,
                )
            )
        return results

    # ── Состояние ────────────────────────────────────────────────────

    def stats(self, collection: str) -> IndexStats:
        """Состояние коллекции для health и UI."""
        try:
            if not self._client.collection_exists(collection):
                return IndexStats(collection=collection, exists=False)
            info = self._client.get_collection(collection)
            count = getattr(info, "points_count", None)
            if count is None:
                count = getattr(getattr(info, "result", None), "points_count", 0)
            return IndexStats(collection=collection, points=int(count or 0), exists=True)
        except Exception as exc:  # noqa: BLE001
            return IndexStats(collection=collection, exists=False, error=str(exc)[:200])

    def health(self) -> bool:
        """Работает ли хранилище."""
        try:
            self._client.get_collections()
        except Exception:  # noqa: BLE001
            logger.debug("Qdrant недоступен", exc_info=True)
            return False
        return True

    def close(self) -> None:
        """Закрыть клиент. Ошибка здесь не должна мешать завершению."""
        try:
            self._client.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Ошибка закрытия Qdrant: %s", exc)


def _match(key: str, value: str) -> models.FieldCondition:
    """Условие «поле равно значению»."""
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def build_filter(filters: SearchFilters | None) -> models.Filter | None:
    """Построить Qdrant-фильтр из бизнес-фильтров.

    Даты норм (п.16) требуют двух условий: effective_from <= дата
    И effective_to > дата (либо effective_to не задан). Именно этот
    предикат и должен попадать в индекс — «найди статью 309» без учёта
    редакции даёт неверный результат.
    """
    if filters is None:
        return None

    must: list[models.Condition] = []
    must_not: list[models.Condition] = []

    for key, value in (
        ("case_id", filters.case_id),
        ("document_id", filters.document_id),
        ("document_type", filters.document_type),
        ("court", filters.court),
        ("category", filters.category),
    ):
        if value:
            must.append(_match(key, value))

    if filters.effective_on:
        # DatetimeRange, а не Range: Range в Qdrant только числовой.
        must.append(
            models.FieldCondition(
                key="effective_from",
                range=models.DatetimeRange(lte=filters.effective_on),
            )
        )
        # Норма, у которой effective_to уже прошёл, на эту дату не действует.
        must_not.append(
            models.FieldCondition(
                key="effective_to",
                range=models.DatetimeRange(lt=filters.effective_on),
            )
        )

    if not must and not must_not:
        return None
    return models.Filter(must=must, must_not=must_not)


def _extract_case_id(chunk_id: str) -> str:
    """Извлечь case_id из chunk_id вида '<case_id>:<n>'.

    Формат зафиксирован ingestion-слоем. Если формат другой, возвращается
    пустая строка — фильтр по делу тогда не сработает, что лучше, чем
    молчаливая фильтрация не по тому делу.
    """
    return chunk_id.split(":", 1)[0] if ":" in chunk_id else ""
