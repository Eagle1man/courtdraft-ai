"""Сохранение состояния фонового задания в базу.

Реестр заданий в памяти — источник быстрых ответов, база — то, что
переживает перезапуск службы. Сбой записи не должен ронять саму работу:
результат уже получен, а состояние можно перечитать.

Модуль отдельный, потому что заданий теперь два вида — формирование
проекта и реестр фактов, — и у каждого должна быть одна и та же
обработка ошибок записи, а не две копии.
"""

from __future__ import annotations

import logging
import uuid

from app.db.repositories import JobRepository
from app.services.case_service import db, session_scope

logger = logging.getLogger(__name__)


async def persist_job_state(job_id: uuid.UUID, **fields: object) -> None:
    """Сохранить состояние задания в базе, не ломая работу."""
    if not db.available:
        return
    try:
        async with session_scope(db.factory) as session:
            await JobRepository(session).update(job_id, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось сохранить состояние задания %s: %s", job_id, exc)
