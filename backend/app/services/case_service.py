"""Сервис дел (Фаза 3, основные эндпоинты п.42).

Слой транзакций: коммит и откат решаются здесь, а не в роутерах.
Так эндпоинт остаётся тонким, а бизнес-правила не расползаются.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.repositories import (
    AuditRepository,
    CaseRepository,
    DocumentRepository,
    new_case,
)
from app.db.session import build_database_url, create_engine_and_session, describe_url
from app.models.entities import Case, CaseStatus

logger = logging.getLogger(__name__)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Транзакция с гарантированным откатом.

    Коммит делает вызывающий код явно: неявный коммит внутри сервиса
    не дал бы возможности объединить несколько операций в одну транзакцию.
    """
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


class CaseService:
    """Операции над делами."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.cases = CaseRepository(session)
        self.documents = DocumentRepository(session)
        self.audit = AuditRepository(session)

    async def create_case(
        self,
        case_number: str,
        court_name: str,
        case_type: str,
        *,
        category: str = "",
        is_synthetic: bool = False,
        student_code: str = "",
        actor: str = "system",
    ) -> Case:
        """Создать дело. Номер должен быть уникален в пределах суда."""
        existing = await self.cases.get_by_number(case_number)
        if existing is not None:
            raise ValueError(f"Дело с номером {case_number} уже существует")

        case = await self.cases.create(
            **new_case(
                case_number,
                court_name,
                case_type,
                category=category,
                is_synthetic=is_synthetic,
                student_code=student_code or "",
            )
        )
        await self.audit.log(
            "case.created",
            case_id=case.id,
            actor=actor,
            case_number=case_number,
            is_synthetic=is_synthetic,
            student_code=student_code or "",
        )
        return case

    async def get_case(self, case_id: uuid.UUID) -> Case | None:
        return await self.cases.get(case_id)

    async def find_empty_case(self, student_code: str) -> Case | None:
        """Пустое дело участника — для повторного нажатия «Создать»."""
        return await self.cases.find_empty_case(student_code)

    async def list_cases(
        self,
        *,
        status: str | None = None,
        student_code: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Case]:
        return await self.cases.list(
            status=status, student_code=student_code, limit=limit, offset=offset
        )

    async def delete_case(self, case_id: uuid.UUID, *, actor: str = "judge") -> str | None:
        """Удалить дело и всё его содержимое. Вернуть номер дела или None.

        Запись журнала пишется до удаления: у `audit_entries` связь с делом
        объявлена как `ON DELETE SET NULL`, поэтому запись «дело удалено»
        остаётся в журнале и после удаления дела. Так удаление не стирает
        след действия — это требование юридического режима проекта,
        а не удобство.

        Каскад по документам, проектам и проверкам обеспечивает БД
        (`ON DELETE CASCADE` + `PRAGMA foreign_keys=ON` для SQLite);
        проверено тестом `test_cascade_delete_works`.
        """
        case = await self.cases.get(case_id)
        if case is None:
            return None

        case_number = case.case_number
        await self.audit.log(
            "case.deleted",
            case_id=case_id,
            actor=actor,
            case_number=case_number,
            is_synthetic=case.is_synthetic,
        )
        await self.cases.delete(case_id)
        return case_number

    async def set_status(
        self, case_id: uuid.UUID, status: str, *, actor: str = "system"
    ) -> Case | None:
        """Сменить статус дела. Значение проверяется по CaseStatus."""
        if status not in {s.value for s in CaseStatus}:
            raise ValueError(f"Неизвестный статус: {status}")

        case = await self.cases.set_status(case_id, status)
        if case:
            await self.audit.log("case.status_changed", case_id=case_id, actor=actor, status=status)
        return case


class Database:
    """Управление подключением к БД на уровне приложения.

    Держит движок и фабрику сессий между запросами: создавать пул на каждый
    запрос дорого, а закрывать его в конце запроса — утечка.
    """

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._factory: async_sessionmaker[AsyncSession] | None = None
        self._url: str = ""
        self._available: bool = False

    async def connect(self, settings: Settings | None = None) -> bool:
        """Подключиться. Отказ не является ошибкой запуска (п.53-подход).

        Приложение обязано стартовать и без БД, сообщив о degraded-режиме.

        Настройки передаются явно: иначе соединение всегда бралось бы из
        глобального .env, и приложение, собранное с другой конфигурацией,
        подключалось бы не к той базе (тесты — самый частый случай).
        """
        cfg = settings or get_settings()

        try:
            manager = create_engine_and_session(cfg)
            engine, factory = await manager.__aenter__()
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось инициализировать БД", exc_info=True)
            self._available = False
            return False

        self._engine = engine
        self._factory = factory
        self._url = build_database_url(cfg)
        self._available = True
        logger.info("БД: %s", describe_url(self._url))
        return True

    async def disconnect(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self._factory = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def engine(self) -> AsyncEngine | None:
        return self._engine

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            raise RuntimeError("БД не инициализирована: вызовите connect() при старте")
        return self._factory

    @property
    def url(self) -> str:
        return self._url

    async def ping(self) -> bool:
        """Проверить соединение для /api/health."""
        if self._engine is None:
            return False
        from app.db.session import check_connection

        return await check_connection(self._engine)


# Единственный экземпляр на приложение.
db = Database()
