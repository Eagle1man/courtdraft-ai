"""Сервис отзывов: сохранение оценки и подготовка выборки для обучения.

Ключевое решение: отзыв сохраняется вместе со СНИМКОМ текста проекта.
Проект перегенерируют, версии меняются, и без снимка оценка «стало хуже»
превращается в утверждение без доказательства — по ней нельзя ни сравнить
модели, ни собрать датасет.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repositories import CaseRepository, DraftRepository, FeedbackRepository
from app.models.entities import Feedback
from app.schemas.feedback import FeedbackIn

logger = logging.getLogger(__name__)


class FeedbackService:
    """Операции над отзывами."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._cfg = settings or get_settings()
        self.feedback = FeedbackRepository(session)
        self.drafts = DraftRepository(session)
        self.cases = CaseRepository(session)

    def _model_name(self) -> str:
        """Имя модели, которой сформирован проект.

        Нужно для сравнения версий модели между собой: отзывы без этого
        поля смешивают оценки разных моделей в одну среднюю.
        """
        provider = str(self._cfg.llm_provider.value)
        if provider == "ollama":
            return f"ollama:{self._cfg.ollama_model}"
        return provider

    async def create(self, payload: FeedbackIn) -> Feedback:
        """Сохранить отзыв вместе со снимком проекта."""
        draft_id = payload.draft_id
        draft_version = 0
        draft_text = ""

        if draft_id is None and payload.case_id is not None:
            latest = await self.drafts.get_for_case(payload.case_id)
            if latest is not None:
                draft_id = latest.id

        if draft_id is not None:
            draft = await self.drafts.get(draft_id)
            if draft is not None:
                draft_version = draft.current_version
                versions = await self.drafts.list_versions(draft_id)
                if versions:
                    draft_text = versions[-1].text

        feedback = Feedback(
            case_id=payload.case_id,
            draft_id=draft_id,
            draft_version=draft_version,
            rating=payload.rating,
            verdict=payload.verdict.value,
            section_name=payload.section_name,
            comment=payload.comment,
            author_role=payload.author_role.value,
            author_name=payload.author_name,
            model_name=self._model_name(),
            draft_text=draft_text,
            is_anonymized=payload.is_anonymized,
        )
        await self.feedback.add(feedback)
        logger.info(
            "Отзыв сохранён: %s/5 %s, роль=%s, дело=%s",
            payload.rating,
            payload.verdict.value,
            payload.author_role.value,
            payload.case_id,
        )
        return feedback

    async def list(
        self,
        *,
        case_id: uuid.UUID | None = None,
        verdict: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Feedback]:
        return await self.feedback.list(
            case_id=case_id, verdict=verdict, limit=limit, offset=offset
        )

    async def stats(self, *, case_id: uuid.UUID | None = None) -> dict[str, Any]:
        return await self.feedback.stats(case_id=case_id)

    async def delete(self, feedback_id: uuid.UUID) -> bool:
        return await self.feedback.delete(feedback_id)

    async def export_rows(
        self, *, case_id: uuid.UUID | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        """Выгрузка для дообучения.

        Формат строки — «что показала модель» + «как оценил человек».
        Поля названы явно, чтобы внешний скрипт не догадывался о смысле
        колонок.
        """
        rows = await self.feedback.list(case_id=case_id, limit=limit)
        result: list[dict[str, Any]] = []

        case_numbers: dict[uuid.UUID, str] = {}
        for item in rows:
            if item.case_id is not None and item.case_id not in case_numbers:
                case = await self.cases.get(item.case_id)
                case_numbers[item.case_id] = case.case_number if case else ""

        for item in rows:
            result.append(
                {
                    "id": str(item.id),
                    "created_at": item.created_at.isoformat() if item.created_at else "",
                    "case_number": case_numbers.get(item.case_id, "") if item.case_id else "",
                    "section_name": item.section_name,
                    "model": item.model_name,
                    "draft_version": item.draft_version,
                    "model_output": item.draft_text,
                    "human_rating": item.rating,
                    "human_verdict": item.verdict,
                    "human_comment": item.comment,
                    "author_role": item.author_role,
                    "anonymized": item.is_anonymized,
                }
            )
        return result
