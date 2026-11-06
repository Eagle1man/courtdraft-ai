"""Реестр фактов дела: извлечение, проверка источников, сохранение (§6–§8).

Порядок здесь важнее кода:

1. **Чтение из базы** — короткая сессия, из неё наружу выходят только
   простые значения. SQLite в этом проекте берёт блокировку записи даже
   на чтение (`BEGIN IMMEDIATE`), поэтому держать транзакцию открытой
   во время вызовов модели нельзя: остальные запросы получат
   «database is locked».
2. **Извлечение** — без транзакции: десятки секунд на документ.
3. **Запись** — снова короткая сессия.

Сбой на любом шаге не должен оставлять реестр «наполовину правильным»:
сохраняется либо готовый реестр, либо ничего, а причина уходит в задание.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.repositories import CaseRepository, DocumentRepository, FactRegistryRepository
from app.extraction.fact_extractor import extract_facts, merge_registries
from app.extraction.schemas import CaseRegistry, ExtractionStatus
from app.ingestion.chunking import chunk_text
from app.models.entities import Case, FactRegistry
from app.services.case_service import db, session_scope
from app.services.job_state import persist_job_state
from app.services.jobs import JobState, jobs
from app.validation.evidence import apply_contradictions, verify_registry

logger = logging.getLogger(__name__)


class CaseHasNoDocumentsError(RuntimeError):
    """В деле нет материалов: извлекать нечего."""


class CaseDeletedError(RuntimeError):
    """Дело удалили, пока шло извлечение. Сохранять снимок не в что."""


#: Извлечение идёт на том же процессоре, что и формирование проекта:
#: у модели один экземпляр, и параллельные прогоны не ускоряют, а
#: сваливают машину в своп. Поэтому один слот, очередь даёт реестр jobs.
_FACTS_SLOTS = asyncio.Semaphore(1)


@dataclass(frozen=True)
class TextChunk:
    """Фрагмент документа в том виде, в каком его ждёт извлечение."""

    text: str
    page: int | None = None
    paragraph: int | None = None


@dataclass(frozen=True)
class DocumentData:
    """Документ без ORM: сессия уже закрыта, объекты не истекли."""

    id: str
    filename: str
    text: str
    chunks: list[TextChunk] = field(default_factory=list)


@dataclass(frozen=True)
class CaseData:
    id: uuid.UUID
    case_number: str
    documents: list[DocumentData]


async def load_case_data(session, case_id: uuid.UUID) -> CaseData:
    """Прочитать дело и материалы в простые структуры.

    Вызывается внутри сессии. Возвращает копии, а не ORM-объекты: после
    выхода из сессии они истекают, и обращение к полям падает.
    """
    case: Case | None = await CaseRepository(session).get(case_id)
    if case is None:
        raise LookupError(f"Дело {case_id} не найдено")

    documents = await DocumentRepository(session).list_for_case(case_id)
    prepared: list[DocumentData] = []

    for document in documents:
        chunks = [
            TextChunk(
                text=chunk.text,
                page=chunk.page,
                paragraph=chunk.paragraph,
            )
            for chunk in document.chunks
            if (chunk.text or "").strip()
        ]
        if not chunks and (document.text or "").strip():
            # Старые документы могли остаться без нарезки: тогда режем
            # здесь, иначе извлечение молча получит пустой контекст.
            chunks = [
                TextChunk(text=piece.text)
                for piece in chunk_text(document.text, document_id=str(document.id))
            ]
        prepared.append(
            DocumentData(
                id=str(document.id),
                filename=document.filename,
                text=document.text or "",
                chunks=chunks,
            )
        )

    return CaseData(id=case.id, case_number=case.case_number, documents=prepared)


def summarize(registry: CaseRegistry, quotes: dict[str, int]) -> dict[str, object]:
    """Счётчики для списка: кэш, источник истины — сам реестр."""
    return {
        "participants": len(registry.participants),
        "amounts": len(registry.amounts),
        "dates": len(registry.dates),
        "claims": len(registry.claims),
        "facts": len(registry.facts),
        "missing": len(registry.missing),
        "contradictions": len(registry.contradictions),
        "quotes": quotes,
    }


async def extract_case_registry(
    data: CaseData,
    provider: object,
    *,
    max_documents: int,
    max_chars: int,
) -> tuple[CaseRegistry, dict[str, int], int]:
    """Извлечь реестр из документов дела и проверить цитаты.

    Возвращает реестр, счётчики проверки цитат и число разобранных
    документов. Модель вызывается здесь, поэтому вызывать эту функцию
    можно только вне транзакции базы.
    """
    documents = [item for item in data.documents if item.chunks][:max_documents]
    if not documents:
        raise CaseHasNoDocumentsError(
            "В материалах дела нет текста для извлечения фактов."
        )

    registries: list[CaseRegistry] = []
    for document in documents:
        run = await extract_facts(
            provider,  # type: ignore[arg-type]
            document.chunks,
            document_id=document.id,
            max_chars=max_chars,
            mode="categories",
        )
        registries.append(run.registry)

    merged = merge_registries(registries)
    if not merged.case_number:
        merged.case_number = data.case_number

    texts = {item.id: item.text for item in documents}
    report = verify_registry(merged, texts)
    apply_contradictions(merged)

    if report.status is ExtractionStatus.NEEDS_REVIEW:
        merged.status = ExtractionStatus.NEEDS_REVIEW

    quotes = {
        "exact": report.exact,
        "normalized": report.normalized,
        "absent": report.absent,
    }
    return merged, quotes, len(documents)


async def save_registry(
    session,
    *,
    case_id: uuid.UUID,
    registry: CaseRegistry,
    quotes: dict[str, int],
    model: str,
    documents: int,
    duration_sec: float,
) -> FactRegistry:
    """Сохранить снимок реестра. Вызывается в короткой сессии."""
    record = FactRegistry(
        case_id=case_id,
        status=registry.status.value,
        model=model,
        documents=documents,
        duration_sec=round(duration_sec, 1),
        payload=registry.model_dump(mode="json"),
        summary=summarize(registry, quotes),
    )
    return await FactRegistryRepository(session).add(record)


async def run_fact_job(
    job_id: uuid.UUID,
    case_id: uuid.UUID,
    cfg: Settings,
    model: str,
) -> None:
    """Фоновая задача: собрать реестр фактов для дела.

    Настройки и модель передаются явно — так же, как при формировании
    проекта: фоновая задача не должна зависеть от того, что успели
    поменять в `.env` после постановки задачи.
    """
    # Слот берётся до перевода задания в RUNNING: пока ждём, оно честно
    # остаётся PENDING, и интерфейс показывает ожидание, а не работу.
    async with _FACTS_SLOTS:
        await jobs.update(job_id, state=JobState.RUNNING, started_at=datetime.now(UTC))
        await persist_job_state(
            job_id, state=JobState.RUNNING.value, started_at=datetime.now(UTC)
        )
        await _run(job_id, case_id, cfg, model, time.perf_counter())


async def _run(
    job_id: uuid.UUID,
    case_id: uuid.UUID,
    cfg: Settings,
    model: str,
    started: float,
) -> None:
    """Реальное извлечение — под слотом."""
    try:
        async with session_scope(db.factory) as session:
            data = await load_case_data(session, case_id)

        from app.llm import build_llm_provider

        provider = build_llm_provider(cfg, model=model)
        registry, quotes, documents = await extract_case_registry(
            data,
            provider,
            max_documents=cfg.facts_max_documents,
            max_chars=cfg.facts_max_chars,
        )

        try:
            async with session_scope(db.factory) as session:
                await save_registry(
                    session,
                    case_id=case_id,
                    registry=registry,
                    quotes=quotes,
                    model=model,
                    documents=documents,
                    duration_sec=time.perf_counter() - started,
                )
        except IntegrityError as exc:
            # Дело удалили, пока извлечение работало: сохранять снимок
            # не в что. Это не сбой системы, а состояние данных.
            raise CaseDeletedError(
                "Дело было удалено во время извлечения фактов."
            ) from exc

        await jobs.update(job_id, state=JobState.DONE, finished_at=datetime.now(UTC))
        await persist_job_state(
            job_id, state=JobState.DONE.value, finished_at=datetime.now(UTC)
        )
    except (CaseHasNoDocumentsError, CaseDeletedError) as exc:
        await jobs.update(
            job_id,
            state=JobState.FAILED,
            finished_at=datetime.now(UTC),
            error=str(exc),
        )
        await persist_job_state(
            job_id,
            state=JobState.FAILED.value,
            finished_at=datetime.now(UTC),
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Извлечение фактов не удалось: %s", job_id)
        message = f"{type(exc).__name__}: {exc}"[:400]
        await jobs.update(
            job_id,
            state=JobState.FAILED,
            finished_at=datetime.now(UTC),
            error=message,
        )
        await persist_job_state(
            job_id,
            state=JobState.FAILED.value,
            finished_at=datetime.now(UTC),
            error=message,
        )
