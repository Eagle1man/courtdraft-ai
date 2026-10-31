"""Сервис проекта судебного акта (Фаза 11, п.26–п.32).

Связывает три ранее независимые части: пакет доказательств, генератор
проекта и проверку. Раньше они существовали только внутри демонстрационного
скрипта, поэтому результат нигде не сохранялся и был недоступен по HTTP.

Порядок записи важен: сначала пакет доказательств, потом проект со ссылкой
на него, потом результат проверки. Если генерация не удалась, пакет всё
равно остаётся — он полезен сам по себе и объясняет, что было в материалах.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.draft_agent import DraftResult, generate_draft
from app.agents.evidence_pack import EvidencePack, build_evidence_pack, load_template
from app.db.repositories import (
    AuditRepository,
    CaseRepository,
    DocumentRepository,
    DraftRepository,
    EvidenceRepository,
)
from app.drafting.sections import resplit_sections
from app.llm.base import LLMProvider
from app.models.entities import (
    Draft,
    DraftStatus,
    DraftVersion,
    EvidenceItem,
    Verification,
)
from app.verification.engine import VerificationReport, verify_draft
from app.verification.recheck import recheck_version

logger = logging.getLogger(__name__)


class CaseNotReadyError(ValueError):
    """В деле нет материалов, из которых можно собрать пакет."""


class DraftService:
    """Формирование, сохранение и проверка проекта акта."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.cases = CaseRepository(session)
        self.documents = DocumentRepository(session)
        self.drafts = DraftRepository(session)
        self.evidence = EvidenceRepository(session)
        self.audit = AuditRepository(session)

    # ── Пакет доказательств ───────────────────────────────────

    async def build_pack(self, case_id: uuid.UUID) -> EvidencePack:
        """Собрать пакет доказательств по делу."""
        case = await self.cases.get(case_id)
        if case is None:
            raise LookupError(f"Дело {case_id} не найдено")

        documents = await self.documents.list_for_case(case.id)
        if not documents:
            raise CaseNotReadyError(
                "В деле нет документов. Проект нельзя составить по пустому делу."
            )

        return build_evidence_pack(
            case.case_number, case.court_name, case.category, documents
        )

    async def save_pack(self, case_id: uuid.UUID, pack: EvidencePack) -> int:
        """Сохранить пакет доказательств в базу."""
        rows = [
            EvidenceItem(
                case_id=case_id,
                category=item.category,
                content=item.text,
                source={
                    "document": item.source_document,
                    "document_type": item.source_type,
                    "page": item.page,
                    "chunk": item.chunk_anchor,
                    "citation": item.citation,
                    "amounts": item.amounts,
                },
                # Факт из материалов дела проверки не требует: проверяется
                # не он, а то, что из него сложено в проект акта.
                requires_review=False,
            )
            for item in pack.items
        ]

        saved = await self.evidence.replace_for_case(case_id, rows)
        await self.audit.log(
            "evidence.saved",
            case_id=case_id,
            actor="system",
            facts=saved,
            gaps=len(pack.gaps),
        )
        return saved

    async def list_evidence(
        self, case_id: uuid.UUID, *, category: str | None = None, limit: int = 200
    ) -> list[EvidenceItem]:
        return await self.evidence.list_for_case(
            case_id, category=category, limit=limit
        )

    # ── Проект акта ───────────────────────────────────────────

    async def generate(
        self,
        case_id: uuid.UUID,
        *,
        provider: LLMProvider | None = None,
        template_name: str = "civil_judgment",
        actor: str = "system",
    ) -> tuple[Draft, DraftVersion, VerificationReport, EvidencePack]:
        """Сформировать проект, сохранить его и результат проверки.

        Возвращает проект, созданную версию, отчёт проверки и пакет —
        всё вместе, потому что вызывающему коду нужно показать пользователю
        и текст, и то, на что он опирается, и что в нём не так.
        """
        pack = await self.build_pack(case_id)
        saved = await self.save_pack(case_id, pack)
        logger.info("Пакет доказательств: %d фактов", saved)

        template = load_template(name=template_name)
        draft_result = await generate_draft(pack, template, provider=provider)

        report = verify_draft(draft_result, pack)

        draft, version = await self._persist(
            case_id,
            draft_result,
            report,
            template_name=template_name,
            actor=actor,
        )

        await self.audit.log(
            "draft.generated",
            case_id=case_id,
            actor=actor,
            draft_id=str(draft.id),
            version=version.version,
            provider=draft_result.provider,
            model=draft_result.model,
            mock=any(s.is_mock for s in draft_result.sections),
            verification=report.status.value,
            unsupported=report.unsupported_claims,
            latency_ms=draft_result.total_latency_ms,
        )

        return draft, version, report, pack

    async def persist_result(
        self,
        case_id: uuid.UUID,
        result: DraftResult,
        report: VerificationReport,
        *,
        template_name: str = "civil_judgment",
        actor: str = "system",
    ) -> tuple[Draft, DraftVersion]:
        """Сохранить уже готовый проект и результат его проверки.

        Нужно демонстрационному прогону: он формирует проект сам, чтобы
        показать в отчёте время по разделам, и затем обязан сохранить
        его в базе — иначе база демо остаётся пустой, а интерфейс на
        ней показывает «Проект не сформирован».

        Генерация при этом не повторяется: сохраняется ровно тот проект,
        который уже проверен, поэтому демо и интерфейс показывают один
        и тот же текст, а не результаты двух независимых прогонов.
        """
        draft, version = await self._persist(
            case_id,
            result,
            report,
            template_name=template_name,
            actor=actor,
        )
        await self.audit.log(
            "draft.generated",
            case_id=case_id,
            actor=actor,
            draft_id=str(draft.id),
            version=version.version,
            provider=result.provider,
            model=result.model,
            mock=any(s.is_mock for s in result.sections),
            verification=report.status.value,
            unsupported=report.unsupported_claims,
            latency_ms=result.total_latency_ms,
        )
        return draft, version

    async def _persist(
        self,
        case_id: uuid.UUID,
        result: DraftResult,
        report: VerificationReport,
        *,
        template_name: str,
        actor: str,
    ) -> tuple[Draft, DraftVersion]:
        """Сохранить проект, его версию и результат проверки.

        Отчёт передаётся готовым, а не вычисляется здесь повторно: проверка
        уже выполнена против настоящего пакета доказательств, и повторный
        прогон против урезанных данных дал бы другой, менее строгий ответ.
        """
        draft = await self.drafts.get_for_case(case_id)
        if draft is None:
            draft = await self.drafts.create(
                case_id, template_name=template_name, status=DraftStatus.DRAFT.value
            )

        sections = [
            {
                "key": s.key,
                "name": s.name,
                "text": s.text,
                "is_mock": s.is_mock,
                "needs_review": s.needs_review,
                "citations": s.citations,
                "latency_ms": s.latency_ms,
                "gaps": s.gaps,
            }
            for s in result.sections
        ]

        needs_review = sum(1 for s in result.sections if s.needs_review)
        version = await self.drafts.add_version(
            draft.id,
            text=result.text,
            author="ai",
            comment=(
                f"Сформировано моделью {result.provider}"
                f"{'/' + result.model if result.model else ''}; "
                f"разделов: {len(result.sections)}, "
                f"требуют проверки: {needs_review}"
            ),
            citations=sections,
        )

        report_status = report.status.value
        await self.drafts.add_verification(
            draft.id,
            draft_version=version.version,
            status=report_status,
            issues=[i.to_dict() for i in report.issues],
            claims_checked=report.claims_checked,
            unsupported_claims=report.unsupported_claims,
            sections_checked=report.sections_checked,
        )

        return draft, version

    # ── Чтение ────────────────────────────────────────────────

    async def get_draft_for_case(self, case_id: uuid.UUID) -> Draft | None:
        return await self.drafts.get_for_case(case_id)

    async def list_versions(self, draft_id: uuid.UUID) -> list[DraftVersion]:
        return await self.drafts.list_versions(draft_id)

    async def get_version(self, draft_id: uuid.UUID, number: int) -> DraftVersion | None:
        return await self.drafts.get_version(draft_id, number)

    async def add_manual_version(
        self,
        draft_id: uuid.UUID,
        *,
        text: str,
        author: str,
        comment: str = "",
        citations: list[Any] | None = None,
        actor: str = "human",
    ) -> DraftVersion:
        """Добавить правку человека.

        Правка сохраняется целиком, а не как разметка изменений: юридический
        акт нужно уметь показать в том виде, в каком он был принят.

        Разбивка на разделы и ссылки на источники переносятся на новый
        текст. Раньше они просто терялись: правка сохранялась с пустым
        списком разделов, и интерфейс показывал «разделов 0 ·
        источников 0» — то есть все ссылки на материалы дела исчезали
        именно тогда, когда судья брал проект в работу. Перенесённые
        разделы помечаются как требующие проверки, если их текст
        изменился: правка человека не проверялась автоматически.
        """
        if author not in {"secretary", "judge"}:
            raise ValueError(
                f"Неизвестный автор правки: {author}. "
                "Допустимы secretary и judge."
            )

        if citations is None:
            versions = await self.drafts.list_versions(draft_id)
            previous = versions[-1].citations if versions else None
            citations = resplit_sections(text, previous)

        version = await self.drafts.add_version(
            draft_id,
            text=text,
            author=author,
            comment=comment,
            citations=citations,
        )
        await self.audit.log(
            "draft.edited",
            actor=actor,
            draft_id=str(draft_id),
            version=version.version,
            author=author,
        )
        return version

    async def reverify(
        self, draft_id: uuid.UUID
    ) -> tuple[Draft, DraftVersion, Verification, VerificationReport]:
        """Проверить последнюю версию заново (Этап 8).

        Правило: исправление не повышает статус само по себе. Статус
        считается только из нового отчёта; старый отчёт остаётся
        привязанным к своей версии и помечается устаревшим.
        """
        draft = await self.drafts.get(draft_id)
        if draft is None:
            raise LookupError("Проект не найден")

        versions = await self.drafts.list_versions(draft_id)
        if not versions:
            raise ValueError("У проекта нет сохранённой версии")

        version = versions[-1]
        pack = await self.build_pack(draft.case_id)
        report = recheck_version(version, pack)

        record = await self.drafts.add_verification(
            draft.id,
            draft_version=version.version,
            status=report.status.value,
            issues=[issue.to_dict() for issue in report.issues],
            claims_checked=report.claims_checked,
            unsupported_claims=report.unsupported_claims,
            sections_checked=report.sections_checked,
        )
        return draft, version, record, report

    async def set_status(
        self, draft_id: uuid.UUID, new_status: str, *, actor: str = "system"
    ) -> Draft | None:
        if new_status not in {s.value for s in DraftStatus}:
            raise ValueError(f"Неизвестный статус проекта: {new_status}")

        draft = await self.drafts.get(draft_id)
        if draft is None:
            return None
        draft.status = new_status
        await self._session.flush()
        await self.audit.log(
            "draft.status_changed",
            actor=actor,
            draft_id=str(draft_id),
            status=new_status,
        )
        return draft
