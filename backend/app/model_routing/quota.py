"""Квота на сильную модель: первым N делам — сильная, остальным — быстрая.

Зачем это в продукте, а не в скрипте теста: на одном CPU-узле нельзя
держать две модели одновременно, а сравнить их на живых людях нужно.
Квота делает сравнение честным — назначение фиксируется за делом и не
меняется при повторной генерации, иначе один и тот же проект могли бы
посчитать то сильной, то слабой моделью.

Назначение хранится в журнале аудита, а не в отдельной таблице. Причина:
это решение системы, которое должно быть видно в истории дела, и оно не
требует миграции схемы на уже работающей ВМ.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import AuditRepository

STRONG_ACTION = "model.assigned.strong"
WEAK_ACTION = "model.assigned.weak"
ASSIGNMENT_ACTIONS = (STRONG_ACTION, WEAK_ACTION)

# Один процесс — одна очередь назначений. Без блокировки два первых
# запроса одновременно увидели бы «сильных ещё нет» и выдали два слота
# сверх квоты.
_ASSIGN_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class ModelAssignment:
    """Какая модель закреплена за делом и почему."""

    case_id: uuid.UUID
    model: str
    tier: str
    slot: int
    existing: bool


class ModelRouter:
    """Выдаёт модель на дело по квоте и помнит решение."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        audit: AuditRepository | None = None,
    ) -> None:
        self._cfg = settings
        self._audit = audit or AuditRepository(session)

    async def assign(self, case_id: uuid.UUID) -> ModelAssignment:
        """Закрепить модель за делом или вернуть уже закреплённую."""
        if not self._cfg.model_routing_enabled:
            return ModelAssignment(
                case_id=case_id,
                model=self._cfg.ollama_model,
                tier="default",
                slot=0,
                existing=False,
            )

        async with _ASSIGN_LOCK:
            current = await self._existing(case_id)
            if current is not None:
                return current

            strong = self._cfg.strong_model or self._cfg.ollama_model
            weak = self._cfg.weak_model or self._cfg.ollama_model
            quota = max(self._cfg.strong_model_quota, 0)
            used = await self._audit.count_by_action(STRONG_ACTION)

            if used < quota and strong != weak:
                action = STRONG_ACTION
                model = strong
                tier = "strong"
                slot = used + 1
            else:
                action = WEAK_ACTION
                model = weak
                tier = "weak"
                slot = 0

            await self._audit.log(
                action,
                case_id=case_id,
                actor="system",
                model=model,
                tier=tier,
                slot=slot,
                quota=quota,
                policy="first_cases",
            )
            return ModelAssignment(
                case_id=case_id,
                model=model,
                tier=tier,
                slot=slot,
                existing=False,
            )

    async def model_for_case(self, case_id: uuid.UUID) -> str | None:
        """Только чтение: какая модель закреплена, без выдачи слота."""
        current = await self._existing(case_id)
        return current.model if current is not None else None

    async def _existing(self, case_id: uuid.UUID) -> ModelAssignment | None:
        entries = await self._audit.list_for_case(case_id, limit=200)
        for entry in entries:
            if entry.action not in ASSIGNMENT_ACTIONS:
                continue
            details: dict[str, Any] = entry.details or {}
            model = str(details.get("model") or "")
            if not model:
                continue
            return ModelAssignment(
                case_id=case_id,
                model=model,
                tier=str(details.get("tier") or self._tier_from_action(entry.action)),
                slot=int(details.get("slot") or 0),
                existing=True,
            )
        return None

    @staticmethod
    def _tier_from_action(action: str) -> str:
        return "strong" if action == STRONG_ACTION else "weak"
