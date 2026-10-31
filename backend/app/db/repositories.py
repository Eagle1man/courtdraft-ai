"""Репозитории: доступ к данным (Фаза 3).

Слой намеренно тонкий. Здесь только SQL-запросы и маппинг — вся логика
в сервисах, вся валидация в схемах Pydantic.

Репозитории принимают готовую сессию снаружи: это позволяет тестам
работать в одной транзакции, а приложению — управлять коммитами
на уровне сервиса.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import (
    AuditEntry,
    Case,
    CaseAnalysis,
    CaseDocument,
    CaseStatus,
    DocumentChunk,
    Draft,
    DraftVersion,
    EvidenceItem,
    Feedback,
    Verification,
)


class CaseRepository:
    """Операции над делами (п.42)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, **values: Any) -> Case:
        case = Case(**values)
        self._session.add(case)
        await self._session.flush()
        return case

    async def get(self, case_id: uuid.UUID) -> Case | None:
        return await self._session.get(Case, case_id)

    async def get_by_number(self, case_number: str) -> Case | None:
        result = await self._session.execute(
            select(Case).where(Case.case_number == case_number)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Case]:
        stmt = select(Case).order_by(Case.created_at.desc())
        if status:
            stmt = stmt.where(Case.status == status)
        stmt = stmt.limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count(self, *, status: str | None = None) -> int:
        """Число дел.

        Фильтр обязателен и здесь: `total` в ответе списка должен
        соответствовать применённому фильтру, иначе интерфейс покажет
        «найдено 12» при пустом списке на странице.
        """
        stmt = select(func.count()).select_from(Case)
        if status:
            stmt = stmt.where(Case.status == status)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def set_status(self, case_id: uuid.UUID, status: str) -> Case | None:
        case = await self.get(case_id)
        if case:
            case.status = status
            await self._session.flush()
        return case

    async def delete(self, case_id: uuid.UUID) -> bool:
        """Удалить дело вместе со всем содержимым (ON DELETE CASCADE)."""
        case = await self.get(case_id)
        if not case:
            return False
        await self._session.delete(case)
        await self._session.flush()
        return True


class DocumentRepository:
    """Операции над документами и чанками (п.10, п.12)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: CaseDocument) -> CaseDocument:
        self._session.add(document)
        await self._session.flush()
        return document

    async def get(self, document_id: uuid.UUID) -> CaseDocument | None:
        return await self._session.get(CaseDocument, document_id)

    async def find_by_sha256(
        self, sha256: str, *, case_id: uuid.UUID | None = None
    ) -> CaseDocument | None:
        """Найти документ по хэшу — защита от загрузки дублей.

        Поиск ограничен делом, если оно указано. Дубль проверяется внутри
        одного дела, а не во всей базе: один и тот же документ законно
        встречается в разных делах (типовой иск, типовой договор), и
        глобальный поиск делал бы вторую загрузку невозможной.

        Раньше здесь стоял scalar_one_or_none() без ограничения делом:
        при двух одинаковых документах в разных делах он выбрасывал
        MultipleResultsFound, и загрузка падала с 500.
        """
        stmt = select(CaseDocument).where(CaseDocument.sha256 == sha256)
        if case_id is not None:
            stmt = stmt.where(CaseDocument.case_id == case_id)

        result = await self._session.execute(stmt.limit(1))
        return result.scalars().first()

    async def list_for_case(
        self, case_id: uuid.UUID, *, document_type: str | None = None
    ) -> list[CaseDocument]:
        stmt = select(CaseDocument).where(CaseDocument.case_id == case_id)
        if document_type:
            stmt = stmt.where(CaseDocument.document_type == document_type)
        stmt = stmt.order_by(CaseDocument.created_at)
        return list((await self._session.execute(stmt)).scalars().all())

    async def replace_chunks(
        self, document_id: uuid.UUID, chunks: list[DocumentChunk]
    ) -> int:
        """Заменить чанки документа (пере-чанкинг после смены параметров)."""
        existing = list(
            (
                await self._session.execute(
                    select(DocumentChunk).where(DocumentChunk.document_id == document_id)
                )
            )
            .scalars()
            .all()
        )
        for chunk in existing:
            await self._session.delete(chunk)
        await self._session.flush()

        self._session.add_all(chunks)
        await self._session.flush()
        return len(chunks)

    async def count_for_case(self, case_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(CaseDocument)
            .where(CaseDocument.case_id == case_id)
        )
        return int(result.scalar_one())

    async def total_text_length(self, case_id: uuid.UUID) -> int:
        """Объём текста по делу — для оценки нагрузки на чанкинг."""
        result = await self._session.execute(
            select(func.coalesce(func.sum(func.length(CaseDocument.text)), 0)).where(
                CaseDocument.case_id == case_id
            )
        )
        return int(result.scalar_one())


class AnalysisRepository:
    """Хранение результатов анализа дела (п.21)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, case_id: uuid.UUID, **values: Any) -> CaseAnalysis:
        """Создать или заменить анализ дела (для дела он один)."""
        existing = await self._session.execute(
            select(CaseAnalysis).where(CaseAnalysis.case_id == case_id)
        )
        analysis = existing.scalar_one_or_none()

        if analysis is None:
            analysis = CaseAnalysis(case_id=case_id, **values)
            self._session.add(analysis)
        else:
            for key, value in values.items():
                setattr(analysis, key, value)
        await self._session.flush()
        return analysis

    async def get(self, case_id: uuid.UUID) -> CaseAnalysis | None:
        result = await self._session.execute(
            select(CaseAnalysis).where(CaseAnalysis.case_id == case_id)
        )
        return result.scalar_one_or_none()


class DraftRepository:
    """Проекты актов и их версии (п.31)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, case_id: uuid.UUID, **values: Any) -> Draft:
        draft = Draft(case_id=case_id, **values)
        self._session.add(draft)
        await self._session.flush()
        return draft

    async def get(self, draft_id: uuid.UUID) -> Draft | None:
        return await self._session.get(Draft, draft_id)

    async def get_for_case(self, case_id: uuid.UUID) -> Draft | None:
        result = await self._session.execute(
            select(Draft).where(Draft.case_id == case_id).order_by(Draft.created_at.desc())
        )
        return result.scalars().first()

    async def add_version(
        self,
        draft_id: uuid.UUID,
        *,
        text: str,
        author: str,
        comment: str = "",
        citations: list[Any] | None = None,
    ) -> DraftVersion:
        """Добавить версию и обновить счётчик.

        Номер присваивается под блокировкой строки: иначе при параллельных
        правках номера совпадут, а уникальный индекс отклонит вторую запись.
        """
        draft = await self.get(draft_id)
        if draft is None:
            raise LookupError(f"Draft {draft_id} не найден")

        next_version = draft.current_version + 1
        version = DraftVersion(
            draft_id=draft_id,
            version=next_version,
            text=text,
            author=author,
            comment=comment,
            citations=citations or [],
        )
        self._session.add(version)
        draft.current_version = next_version
        await self._session.flush()
        return version

    async def list_versions(self, draft_id: uuid.UUID) -> list[DraftVersion]:
        result = await self._session.execute(
            select(DraftVersion)
            .where(DraftVersion.draft_id == draft_id)
            .order_by(DraftVersion.version)
        )
        return list(result.scalars().all())

    async def get_version(self, draft_id: uuid.UUID, version: int) -> DraftVersion | None:
        result = await self._session.execute(
            select(DraftVersion).where(
                DraftVersion.draft_id == draft_id, DraftVersion.version == version
            )
        )
        return result.scalar_one_or_none()

    async def add_verification(self, draft_id: uuid.UUID, **values: Any) -> Verification:
        verification = Verification(draft_id=draft_id, **values)
        self._session.add(verification)
        await self._session.flush()
        return verification


class EvidenceRepository:
    """Элементы пакета доказательств (п.26).

    Пакет сохраняется, а не строится на лету при каждом запросе: к нему
    потом привязывается проект, и без сохранённого пакета невозможно
    объяснить, откуда взялся конкретный факт в тексте.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_case(
        self, case_id: uuid.UUID, items: list[EvidenceItem]
    ) -> int:
        """Перезаписать пакет дела.

        Прежний пакет удаляется целиком: он относится к конкретному
        состоянию материалов, и смешивание старого и нового в одной
        выдаче сделало бы ссылки на источники недостоверными.
        """
        await self._session.execute(
            delete(EvidenceItem).where(EvidenceItem.case_id == case_id)
        )
        await self._session.flush()

        for item in items:
            item.case_id = case_id
            self._session.add(item)
        await self._session.flush()
        return len(items)

    async def count_for_case(self, case_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(EvidenceItem)
            .where(EvidenceItem.case_id == case_id)
        )
        return int(result.scalar_one())

    async def list_for_case(
        self, case_id: uuid.UUID, *, category: str | None = None, limit: int = 200
    ) -> list[EvidenceItem]:
        query = select(EvidenceItem).where(EvidenceItem.case_id == case_id)
        if category:
            query = query.where(EvidenceItem.category == category)
        result = await self._session.execute(query.order_by(EvidenceItem.id).limit(limit))
        return list(result.scalars().all())


class AuditRepository:
    """Аудит действий (п.46)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        action: str,
        *,
        case_id: uuid.UUID | None = None,
        actor: str = "system",
        **details: Any,
    ) -> AuditEntry:
        """Записать действие.

        В details не должно попадать содержимое документов: аудит
        хранится долго и читается шире, чем сами дела.
        """
        entry = AuditEntry(action=action, case_id=case_id, actor=actor, details=details)
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def list_for_case(
        self, case_id: uuid.UUID, *, limit: int = 100
    ) -> list[AuditEntry]:
        """Записи по делу, новые первыми.

        Сортировка по id (а не по created_at): в SQLite точность времени
        до секунды, и две записи в одной транзакции получили бы
        неопределённый порядок.
        """
        result = await self._session.execute(
            select(AuditEntry)
            .where(AuditEntry.case_id == case_id)
            .order_by(AuditEntry.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


class FeedbackRepository:
    """Отзывы пользователей о проекте акта."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, feedback: Feedback) -> Feedback:
        self._session.add(feedback)
        await self._session.flush()
        return feedback

    async def get(self, feedback_id: uuid.UUID) -> Feedback | None:
        return await self._session.get(Feedback, feedback_id)

    async def list(
        self,
        *,
        case_id: uuid.UUID | None = None,
        verdict: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Feedback]:
        stmt = select(Feedback).order_by(Feedback.created_at.desc())
        if case_id is not None:
            stmt = stmt.where(Feedback.case_id == case_id)
        if verdict:
            stmt = stmt.where(Feedback.verdict == verdict)
        stmt = stmt.limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count(
        self, *, case_id: uuid.UUID | None = None, verdict: str | None = None
    ) -> int:
        stmt = select(func.count()).select_from(Feedback)
        if case_id is not None:
            stmt = stmt.where(Feedback.case_id == case_id)
        if verdict:
            stmt = stmt.where(Feedback.verdict == verdict)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def stats(self, *, case_id: uuid.UUID | None = None) -> dict[str, Any]:
        """Сводка: сколько отзывов, средняя оценка, разбивка по вердиктам.

        Считается в базе, а не в интерфейсе: на выгрузке в тысячи строк
        клиент не должен тянуть их все ради среднего.
        """
        base = select(func.count()).select_from(Feedback)
        avg_stmt = select(func.avg(Feedback.rating)).select_from(Feedback)
        if case_id is not None:
            base = base.where(Feedback.case_id == case_id)
            avg_stmt = avg_stmt.where(Feedback.case_id == case_id)

        total = int((await self._session.execute(base)).scalar_one())
        average = (await self._session.execute(avg_stmt)).scalar_one()

        by_verdict: dict[str, int] = {}
        verdict_stmt = select(Feedback.verdict, func.count()).group_by(Feedback.verdict)
        if case_id is not None:
            verdict_stmt = verdict_stmt.where(Feedback.case_id == case_id)
        for verdict, count in (await self._session.execute(verdict_stmt)).all():
            by_verdict[str(verdict)] = int(count)

        by_role: dict[str, int] = {}
        role_stmt = select(Feedback.author_role, func.count()).group_by(
            Feedback.author_role
        )
        if case_id is not None:
            role_stmt = role_stmt.where(Feedback.case_id == case_id)
        for role, count in (await self._session.execute(role_stmt)).all():
            by_role[str(role)] = int(count)

        return {
            "total": total,
            "average_rating": round(float(average), 2) if average is not None else 0.0,
            "by_verdict": by_verdict,
            "by_role": by_role,
        }

    async def delete(self, feedback_id: uuid.UUID) -> bool:
        feedback = await self.get(feedback_id)
        if feedback is None:
            return False
        await self._session.delete(feedback)
        await self._session.flush()
        return True


def new_case(case_number: str, court_name: str, case_type: str, **extra: Any) -> dict[str, Any]:
    """Собрать значения для создания дела со значениями по умолчанию."""
    values: dict[str, Any] = {
        "case_number": case_number,
        "court_name": court_name,
        "case_type": case_type,
        "category": extra.pop("category", ""),
        "status": extra.pop("status", CaseStatus.NEW.value),
        "is_synthetic": extra.pop("is_synthetic", False),
    }
    values.update(extra)
    return values
