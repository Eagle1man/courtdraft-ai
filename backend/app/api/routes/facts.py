"""Реестр фактов дела: запуск извлечения и чтение снимка (§6–§8).

Эндпоинты намеренно разделены на «запустить» и «прочитать»:

* POST не ждёт модель. Извлечение по всем документам дела — это минуты
  на процессоре, и держать HTTP-запрос открытым всё это время нельзя:
  прокси и браузер сдадутся раньше.
* GET отдаёт последний сохранённый снимок. Он не пересчитывается на
  каждый показ: пользователь должен видеть то же, что видел в прошлый
  раз, а не новый результат с другим набором фактов.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.api.routes.drafts import SessionDep
from app.config import Settings, get_settings
from app.db.repositories import (
    CaseRepository,
    DocumentRepository,
    FactRegistryRepository,
    JobRepository,
)
from app.schemas.facts import CaseFactsOut, FactsJobOut
from app.services.fact_service import run_fact_job
from app.services.jobs import jobs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["facts"])


def _settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


@router.post(
    "/cases/{case_id}/facts",
    response_model=FactsJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_facts(
    request: Request,
    case_id: uuid.UUID,
    background: BackgroundTasks,
    session: SessionDep,
) -> FactsJobOut:
    """Запустить извлечение фактов по материалам дела."""
    cfg = _settings(request)
    if not cfg.facts_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Извлечение фактов выключено на этом стенде. "
                "Включается настройкой FACTS_ENABLED."
            ),
        )

    case = await CaseRepository(session).get(case_id)
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено"
        )

    documents = await DocumentRepository(session).count_for_case(case_id)
    if documents == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="В деле нет материалов: извлекать факты не из чего.",
        )

    model = cfg.facts_model or cfg.ollama_model

    # Как и с проектом: повторное нажатие не ставит второе извлечение —
    # оно занимает процессор на минуты, и дубли только удлиняют очередь.
    active = await JobRepository(session).find_active(case_id, kind="facts")
    if active is not None:
        return FactsJobOut(
            job_id=active.id,
            case_id=case_id,
            status=active.state,
            started_at=active.created_at,
        )

    job = await jobs.create(case_id)
    await JobRepository(session).create(
        job_id=job.id,
        case_id=case_id,
        state=job.state.value,
        created_at=job.created_at,
        kind="facts",
    )
    # Коммит до фоновой задачи: иначе она увидит незакоммиченную запись
    # задания и не сможет обновить её состояние.
    await session.commit()

    background.add_task(run_fact_job, job.id, case_id, cfg, model)
    return FactsJobOut(
        job_id=job.id,
        case_id=case_id,
        status=job.state.value,
        started_at=job.created_at,
    )


@router.get("/cases/{case_id}/facts", response_model=CaseFactsOut)
async def get_facts(case_id: uuid.UUID, session: SessionDep) -> CaseFactsOut:
    """Последний реестр фактов дела."""
    record = await FactRegistryRepository(session).latest_for_case(case_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Реестр фактов для этого дела ещё не собирался.",
        )
    return CaseFactsOut(
        id=record.id,
        case_id=record.case_id,
        status=record.status,
        model=record.model,
        documents=record.documents,
        duration_sec=record.duration_sec,
        summary=record.summary or {},
        registry=record.payload or {},
        created_at=record.created_at,
    )
