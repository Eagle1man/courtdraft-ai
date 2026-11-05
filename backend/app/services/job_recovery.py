"""Возобновление заданий, прерванных перезапуском службы.

Раньше перезапуск терял работу целиком: задание жило в памяти процесса,
и после рестарта от него оставалась только отметка «прервано». На живом
тесте так пропало около 34 заданий — человек нажимал «сформировать»,
ждал несколько минут и получал отказ, хотя текст можно было получить.

Теперь состояние задания лежит в базе, и после старта служба сама
берётся за незавершённые: `PENDING` (не успело начаться) и `RUNNING`
(оборвалось в середине). Результат генерации сохраняется в конце, поэтому
повтор не плодит версии проекта. Единственное исключение — обрыв между
сохранением версии и отметкой «готово»: его проверяет `_saved_work`,
чтобы не сделать вторую копию того же текста.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from app.db.repositories import DraftRepository, InterruptedJob
from app.model_routing import ModelRouter
from app.services.case_service import db, session_scope
from app.services.jobs import Job, JobState, jobs

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SavedWork:
    """Версия проекта, сохранённая до обрыва задания."""

    draft_id: uuid.UUID
    version: int
    verification: str


def _as_utc(value: datetime | None) -> datetime | None:
    """Привести время к UTC.

    SQLite отдаёт `func.now()` без зоны, PostgreSQL — с зоной. Сравнивать
    такие значения напрямую нельзя: Python бросит TypeError, и задание
    не возобновится из-за формата времени, а не из-за своей сути.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_result_of_job(
    *,
    author: str,
    created_at: datetime | None,
    started_at: datetime | None,
) -> bool:
    """Создана ли версия проекта именно этим заданием.

    Правка человеком тоже создаёт версию, но она не означает, что задание
    выполнилось. Отсекаем её по автору, а по времени — версии, сохранённые
    до начала работы задания.
    """
    if author != "ai" or started_at is None or created_at is None:
        return False
    started = _as_utc(started_at)
    created = _as_utc(created_at)
    if started is None or created is None:
        return False
    return created >= started


async def _saved_work(item: InterruptedJob) -> SavedWork | None:
    """Найти версию, созданную этим заданием до обрыва.

    Если версия есть и она новее начала работы — генерация фактически
    завершилась, просто служба не успела это записать. Повторный запуск
    создал бы вторую копию того же проекта.
    """
    if item.started_at is None or not db.available:
        return None

    try:
        async with session_scope(db.factory) as session:
            drafts = DraftRepository(session)
            draft = await drafts.get_for_case(item.case_id)
            if draft is None:
                return None
            versions = await drafts.list_versions(draft.id)
            if not versions:
                return None
            latest = versions[-1]
            if not _is_result_of_job(
                author=latest.author,
                created_at=latest.created_at,
                started_at=item.started_at,
            ):
                return None
            report = await drafts.get_verification(draft.id, latest.version)
            return SavedWork(
                draft_id=draft.id,
                version=latest.version,
                verification=report.status if report is not None else "",
            )
    except Exception as exc:  # noqa: BLE001
        # Проверка не должна мешать возобновлению: худший исход — лишняя
        # версия проекта, а не потерянная работа.
        logger.warning("Не удалось проверить сохранённую версию дела %s: %s", item.case_id, exc)
        return None


async def _model_for_case(cfg: Settings, case_id: uuid.UUID) -> str:
    """Модель, закреплённая за делом при постановке задания.

    `assign` сначала ищет уже выданную модель и возвращает её же, поэтому
    возобновление не отнимает сильную модель у другого дела.
    """
    if not db.available:
        return cfg.ollama_model
    try:
        async with session_scope(db.factory) as session:
            assignment = await ModelRouter(session, cfg).assign(case_id)
        return assignment.model
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось восстановить модель для дела %s: %s", case_id, exc)
        return cfg.ollama_model


async def _mark_done(job_id: uuid.UUID, saved: SavedWork) -> None:
    from app.api.routes.drafts import persist_job_state

    await jobs.update(
        job_id,
        state=JobState.DONE,
        finished_at=datetime.now(UTC),
        result_draft_id=saved.draft_id,
        result_version=saved.version,
        verification_status=saved.verification,
    )
    await persist_job_state(
        job_id,
        state=JobState.DONE.value,
        finished_at=datetime.now(UTC),
        result_draft_id=saved.draft_id,
        result_version=saved.version,
        verification_status=saved.verification,
    )


async def _mark_failed(job_id: uuid.UUID, reason: str) -> None:
    from app.api.routes.drafts import persist_job_state

    message = reason[:400]
    await jobs.update(job_id, state=JobState.FAILED, finished_at=datetime.now(UTC), error=message)
    await persist_job_state(
        job_id, state=JobState.FAILED.value, finished_at=datetime.now(UTC), error=message
    )


async def resume_one(cfg: Settings, item: InterruptedJob) -> str:
    """Возобновить одно задание. Возвращает исход для журнала и тестов."""
    from app.api.routes.drafts import run_generation

    await jobs.adopt(
        Job(
            id=item.job_id,
            case_id=item.case_id,
            state=JobState.PENDING,
            created_at=item.created_at,
        )
    )

    saved = await _saved_work(item)
    if saved is not None:
        await _mark_done(item.job_id, saved)
        logger.info(
            "Задание %s уже было выполнено до перезапуска: версия %d",
            item.job_id,
            saved.version,
        )
        return "already-done"

    model = await _model_for_case(cfg, item.case_id)
    logger.warning(
        "Возобновляю задание %s по делу %s (попытка %d)",
        item.job_id,
        item.case_id,
        item.attempts + 1,
    )
    # Задание создал пользователь, а не служба: в журнале это видно, чтобы
    # восстановление не выглядело как чья-то посторонняя работа.
    await run_generation(item.job_id, item.case_id, "system", cfg, model)
    return "resumed"


async def resume_interrupted(cfg: Settings, interrupted: list[InterruptedJob]) -> int:
    """Возобновить пачку заданий. Возвращает число принятых в работу."""
    if not interrupted:
        return 0

    logger.warning("После перезапуска возобновляется заданий: %d", len(interrupted))
    results = await asyncio.gather(
        *(resume_one(cfg, item) for item in interrupted), return_exceptions=True
    )

    accepted = 0
    for item, result in zip(interrupted, results, strict=True):
        if isinstance(result, BaseException):
            logger.error(
                "Задание %s не возобновлено: %s", item.job_id, result, exc_info=result
            )
            await _mark_failed(item.job_id, f"Возобновление не удалось: {result}")
            continue
        accepted += 1
    return accepted
