"""Фоновые задачи формирования проекта (MVP-уровень).

Почему вообще нужен фон: формирование проекта на процессоре занимает
две-полторы минуты. Синхронный ответ клиенту за это время невозможен —
истёк бы таймаут, и пользователь решил бы, что сервис завис.

ЧЕГО ЭТОТ РЕГИСТР НЕ УМЕЕТ, и это важно знать до промышленной эксплуатации:

- состояние задач здесь только в памяти. Источник истины — таблица `jobs`
  (см. `JobRecord`): после перезапуска службы незавершённые задания
  возвращаются на повтор (`app.services.job_recovery`), а не теряются.
  Чтение статуса падает в базу, если задания нет в реестре.
- при нескольких рабочих процессах (uvicorn --workers > 1) задача,
  принятая одним процессом, не видна другому: опрос попадёт не туда.
  Поэтому возобновление заданий рассчитано на один процесс.

Для MVP на одном процессе это приемлемо и осознанно. Для промышленного
развёртывания нужен брокер задач и общее хранилище статусов.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

logger = logging.getLogger(__name__)


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass
class Job:
    """Одна фоновая задача."""

    id: uuid.UUID
    case_id: uuid.UUID
    state: JobState = JobState.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Человекочитаемая причина сбоя: сырое исключение пользователю ни о чём
    # не говорит, а «модель не ответила» говорит.
    error: str = ""
    result_draft_id: uuid.UUID | None = None
    result_version: int | None = None
    verification_status: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": str(self.id),
            "case_id": str(self.case_id),
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error": self.error,
            "draft_id": str(self.result_draft_id) if self.result_draft_id else None,
            "version": self.result_version,
            "verification": self.verification_status,
        }


class JobRegistry:
    """Задачи в памяти процесса.

    Ограничен по размеру: иначе долгоживущий сервер со временем накопит
    все когда-либо выполненные задачи и утечёт память.
    """

    def __init__(self, *, capacity: int = 200) -> None:
        self._jobs: dict[uuid.UUID, Job] = {}
        self._capacity = capacity
        self._order: list[uuid.UUID] = []
        self._lock = asyncio.Lock()

    async def create(self, case_id: uuid.UUID) -> Job:
        job = Job(id=uuid.uuid4(), case_id=case_id)
        async with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            await self._evict_locked()
        return job

    async def get(self, job_id: uuid.UUID) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def adopt(self, job: Job) -> Job:
        """Принять задание, поставленное до перезапуска процесса.

        После рестарта реестр пуст, а задание живо в базе. Без этой записи
        опрос статуса шёл бы в базу, а обновления из фоновой задачи молча
        пропадали бы: `update` для неизвестного задания возвращает None.
        """
        async with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            await self._evict_locked()
        return job

    async def update(self, job_id: uuid.UUID, **fields: object) -> Job | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            return job

    async def _evict_locked(self) -> None:
        while len(self._order) > self._capacity:
            oldest = self._order.pop(0)
            self._jobs.pop(oldest, None)

    def count(self) -> int:
        return len(self._jobs)


# Один реестр на процесс — иначе опрос статуса попадёт не туда.
jobs = JobRegistry()
