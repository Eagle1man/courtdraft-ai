"""Эндпоинты отзывов о проекте акта.

Зачем это в продукте, а не в почте разработчика: программа будет
тестироваться студентами и преподавателями юридической академии, и
отзывы — единственный способ узнать, где модель ошибается на реальных
глазах. Отзыв сразу привязывается к делу, разделу и версии проекта,
поэтому потом из него собирается датасет для дообучения.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.schemas.feedback import (
    FeedbackIn,
    FeedbackListOut,
    FeedbackOut,
    FeedbackStatsOut,
)
from app.services.case_service import db, session_scope
from app.services.feedback_service import FeedbackService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


async def get_session() -> AsyncIterator[AsyncSession]:
    if not db.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="База данных недоступна. Проверьте подключение в .env (DATABASE_URL).",
        )
    async with session_scope(db.factory) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def settings_of(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


@router.post("", response_model=FeedbackOut, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    request: Request, payload: FeedbackIn, session: SessionDep
) -> FeedbackOut:
    """POST /api/feedback — сохранить отзыв.

    Дело и проект искать не нужно: если они не переданы, отзыв всё равно
    сохранится как оценка продукта в целом.
    """
    service = FeedbackService(session, settings_of(request))
    feedback = await service.create(payload)
    return FeedbackOut.model_validate(feedback)


@router.get("", response_model=FeedbackListOut)
async def list_feedback(
    session: SessionDep,
    case_id: Annotated[uuid.UUID | None, Query()] = None,
    verdict: Annotated[str | None, Query(max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FeedbackListOut:
    """GET /api/feedback — последние отзывы и сводка по ним."""
    service = FeedbackService(session)
    items = await service.list(
        case_id=case_id, verdict=verdict, limit=limit, offset=offset
    )
    stats = await service.stats(case_id=case_id)
    return FeedbackListOut(
        items=[FeedbackOut.model_validate(item) for item in items],
        total=await service.feedback.count(case_id=case_id, verdict=verdict),
        stats=FeedbackStatsOut(**stats),
    )


@router.get("/stats", response_model=FeedbackStatsOut)
async def feedback_stats(
    session: SessionDep,
    case_id: Annotated[uuid.UUID | None, Query()] = None,
) -> FeedbackStatsOut:
    """GET /api/feedback/stats — сколько отзывов и какая средняя оценка."""
    service = FeedbackService(session)
    return FeedbackStatsOut(**await service.stats(case_id=case_id))


@router.get("/export", response_class=PlainTextResponse)
async def export_feedback(
    session: SessionDep,
    case_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 5000,
) -> PlainTextResponse:
    """GET /api/feedback/export — выгрузка JSONL для дообучения.

    Отдаётся файлом, а не JSON-массивом: строку JSONL можно дописывать
    и читать потоково, а массив из десятков тысяч проектов целиком в
    память браузера не помещается.
    """
    service = FeedbackService(session)
    rows = await service.export_rows(case_id=case_id, limit=limit)
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    filename = f"feedback-{stamp}.jsonl"
    return PlainTextResponse(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(feedback_id: uuid.UUID, session: SessionDep) -> Response:
    """DELETE /api/feedback/{id} — убрать ошибочный или тестовый отзыв."""
    service = FeedbackService(session)
    if not await service.delete(feedback_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Отзыв не найден"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
