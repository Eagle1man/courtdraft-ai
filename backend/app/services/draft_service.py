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
from app.agents.evidence_pack import (
    EvidencePack,
    build_evidence_pack,
    load_template,
    template_name_for_case,
)
from app.config import get_settings
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
from app.rag.laws import law_context_for_case, materials_law_context
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
        """Собрать пакет доказательств по делу.

        Нормы из базы знаний здесь **не** подмешиваются: поиск норм — это
        сетевой вызов эмбеддингов, а пакет собирается внутри транзакции
        SQLite. `BEGIN IMMEDIATE` берёт блокировку записи на любой запрос,
        включая чтение, поэтому вызов модели внутри транзакции держал её
        десятки секунд, и остальные запросы падали с «database is locked».
        Нормы добавляет `apply_law_context` — после закрытия транзакции.
        """
        case = await self.cases.get(case_id)
        if case is None:
            raise LookupError(f"Дело {case_id} не найдено")

        documents = await self.documents.list_for_case(case.id)
        if not documents:
            raise CaseNotReadyError(
                "В деле нет документов. Проект нельзя составить по пустому делу."
            )

        cfg = get_settings()
        return build_evidence_pack(
            case.case_number,
            case.court_name,
            case.category,
            documents,
            case_type=case.case_type,
            filing_date=getattr(case, "filing_date", None),
            settings=cfg,
        )

    async def apply_law_context(self, pack: EvidencePack) -> None:
        """Подмешать нормы к пакету — только вне транзакции базы.

        При включённом RAG берём нормы из базы знаний (по запросу дела).
        Всегда добавляем детерминированный контекст норм, реально
        упомянутых в материалах (без rag_laws_enabled вслепую): он
        подсказывает модели профиль дела и не зависит от эмбеддингов.
        """
        cfg = get_settings()
        rag_blocks: list[str] = []
        if cfg.rag_laws_enabled:
            query = " ".join(
                item.text
                for item in pack.items
                if item.category in {"claims", "objections", "evidence", "established_facts"}
            )[:1500]
            rag_blocks = await law_context_for_case(query, cfg)
        material_blocks = materials_law_context(pack, max_chars=cfg.rag_laws_context_chars)
        # RAG (если есть) даёт тексты норм, материалы — перечень упомянутых;
        # сохраняем оба, избегая точных дублей блоков.
        blocks: list[str] = []
        for block in [*rag_blocks, *material_blocks]:
            if block and block not in blocks:
                blocks.append(block)
        pack.law_context = blocks

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

        # Stage 1.2 dual-write: traceable evidence spans/amount candidates (additive, behind flags).
        try:
            from app.services.case_evidence_trace import dual_write_evidence_pack

            docs = await self.documents.list_for_case(case_id)
            by_name = {d.filename: d.id for d in docs}
            trace = await dual_write_evidence_pack(
                self._session, case_id, pack, document_ids_by_filename=by_name, settings=get_settings()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dual-write evidence trace skipped: %s", exc)
            trace = {"enabled": 0, "error": str(exc)[:200]}

        await self.audit.log(
            "evidence.saved",
            case_id=case_id,
            actor="system",
            facts=saved,
            gaps=len(pack.gaps),
            trace=trace,
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
        template_name: str | None = None,
        actor: str = "system",
        document_kind: str | None = None,
        claim_data: dict[str, Any] | None = None,
    ) -> tuple[Draft, DraftVersion, VerificationReport, EvidencePack]:
        """Сформировать проект, сохранить его и результат проверки.

        Шаблон выбирается по виду судопроизводства дела (``case_type``),
        если он не задан явно. Для искового заявления (``document_kind='claim'``
        или шаблон ``civil_claim``) используется отдельный профиль генерации
        и проверки; функционал судьи (решение) не меняется.
        """
        case = await self.cases.get(case_id)
        if case is None:
            raise LookupError(f"Дело {case_id} не найдено")
        pack = await self.build_pack(case_id)
        # Транзакция закрывается ДО сетевого вызова: поиск норм занимает
        # секунды, и всё это время блокировка записи SQLite держалась бы,
        # останавливая список дел, загрузку документов и создание новых.
        await self._session.commit()
        await self.apply_law_context(pack)

        saved = await self.save_pack(case_id, pack)
        # Коммит до вызова модели обязателен. Иначе транзакция записи
        # (пакет доказательств) остаётся открытой на все 3–4 минуты
        # генерации и блокирует создание дел, загрузку и отзывы —
        # SQLite держит блокировку записи до конца транзакции.
        await self._session.commit()
        logger.info("Пакет доказательств: %d фактов", saved)

        effective_document_kind = (document_kind or "").strip().lower()
        effective_template_name = template_name
        if not effective_document_kind:
            if effective_template_name == "civil_claim" or claim_data is not None:
                effective_document_kind = "claim"
            else:
                effective_document_kind = "judgment"
        if effective_document_kind == "claim" and not effective_template_name:
            effective_template_name = "civil_claim"
        if not effective_template_name:
            effective_template_name = template_name_for_case(case.case_type)

        template = load_template(name=effective_template_name)
        cfg = get_settings()
        trace_context = None
        try:
            from app.services.case_trace_context import load_case_trace_context

            trace_context = await load_case_trace_context(self._session, case_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Trace context unavailable: %s", exc)
        # Stage 4: attach compact traceable ledger/evidence summary for generation (no full-case prompt)
        if isinstance(trace_context, dict):
            try:
                setattr(pack, "trace_context", trace_context)
            except Exception:
                pass
            lines = ["[TRACE_LEDGER_SUMMARY]"]
            prov = trace_context.get("provenance") or {}
            lines.append(
                f"spans={prov.get('spans_count',0)} facts={prov.get('facts_count',0)} amount_items={prov.get('amount_items_count',0)} untraced_amounts={prov.get('has_untraced_amounts')}"
            )
            ac = trace_context.get("amount_conflicts") or {}
            if ac.get("has_conflicts"):
                lines.append("AMOUNT_CONFLICTS=TRUE (требуется проверка сумм/итогов по первоисточникам)")
            items = ((trace_context.get("amount_ledger") or {}).get("items") or [])[:20]
            for it in items:
                money = (it.get("money") or {}).get("value")
                lines.append(
                    f"- amount[{it.get('amount_type')}]={money} {it.get('currency')} status={it.get('status')} "
                    f"trace={'yes' if it.get('has_trace') else 'no'} conflict={'yes' if it.get('conflict') else 'no'} "
                    f"spans={len(it.get('evidence_span_ids') or [])} id={str(it.get('amount_item_id'))[:8]}"
                )
            # include a few evidence span refs (provenance anchors)
            for sp in (trace_context.get("evidence_spans") or [])[:8]:
                ref = f"doc={sp.get('document_id')} page={sp.get('page_number')} span={sp.get('span_id') or sp.get('evidence_span_id')}"
                quote = (sp.get("quote") or "").replace("\n", " ")[:120]
                lines.append(f"- evidence_ref {ref}: {quote}")
            block = "\n".join(lines)
            try:
                pack.law_context = [b for b in (getattr(pack, "law_context", None) or []) if b] + [block]
            except Exception:
                pass
        harness: dict[str, Any] | None = None
        # Чтение trace-контекста выше снова открыло транзакцию (BEGIN
        # IMMEDIATE берёт блокировку записи сразу, даже на чтение). Если её
        # не закрыть, блокировка держится всё время генерации — минуты, —
        # и остальные запросы получают «database is locked» по busy_timeout.
        # Коммит здесь ничего не меняет в данных: пакет уже сохранён.
        await self._session.commit()
        if effective_document_kind == "claim":
            from app.db.repositories import CasePassportRepository, DeviationRepository
            from app.drafting.claim_draft import generate_claim_draft, verify_claim_draft
            from app.ingestion.passport import CasePassport, find_passport_in_pack

            passport_record = None
            repo = CasePassportRepository(self._session)
            record = await repo.get_for_case(case_id)
            passport = None
            if record is not None:
                try:
                    passport = CasePassport.model_validate(record.payload)
                except Exception:  # noqa: BLE001
                    passport = None
            if passport is None:
                passport = find_passport_in_pack(pack)
                if passport is not None:
                    issues = passport.validate_invariants()
                    await repo.upsert(
                        case_id,
                        schema_id=passport.schema_id,
                        mode=passport.mode,
                        status=passport.status,
                        payload=passport.as_dict(),
                        issues=[i.model_dump() for i in issues],
                        source="upload",
                    )
            passport_record = passport
            await self._session.commit()

            draft_result = await generate_claim_draft(
                pack,
                template,
                provider=provider,
                settings=cfg,
                case=case,
                claim_data=claim_data,
            )
            report = verify_claim_draft(draft_result, pack, claim_data=claim_data, passport=passport_record)
            if passport_record is not None:
                from app.drafting.claim_model import ClaimModel
                from app.verification.deviations import build_deviations

                model = ClaimModel.from_passport(passport_record.as_dict())
                rows = build_deviations(model, passport_record)
                await DeviationRepository(self._session).replace_for_case(case_id, rows)
        elif cfg.multi_agent_enabled:
            from app.agents.pipeline import run_pipeline

            pipeline = await run_pipeline(
                pack,
                template,
                provider=provider,
                settings=cfg,
                trace_context=trace_context,
            )
            draft_result = pipeline.draft
            report = pipeline.verification
            harness = getattr(pipeline, "harness", None)
        else:
            draft_result = await generate_draft(pack, template, provider=provider)
            report = verify_draft(draft_result, pack, trace_context=trace_context)
            if getattr(cfg, "model_harness_enabled", False):
                try:
                    from app.harnesses.integration import run_harness_capabilities

                    harness = await run_harness_capabilities(
                        pack=pack,
                        template=template,
                        draft=draft_result,
                        report=report,
                        settings=cfg,
                        trace_context=trace_context if isinstance(trace_context, dict) else None,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Harness integration (non-pipeline) failed: %s", exc)
                    harness = {"enabled": True, "error": str(exc), "capabilities": {}, "warnings": [str(exc)]}

        draft, version = await self._persist(
            case_id,
            draft_result,
            report,
            template_name=str(template.get("name") or ""),
            document_kind=effective_document_kind,
            actor=actor,
            harness=harness if harness and harness.get("enabled") else None,
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
            document_kind=effective_document_kind,
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
        document_kind: str | None = None,
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
        kind = (document_kind or ("claim" if template_name == "civil_claim" else "judgment")).strip().lower() or "judgment"
        draft, version = await self._persist(
            case_id,
            result,
            report,
            template_name=template_name,
            document_kind=kind,
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
            document_kind=kind,
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
        document_kind: str = "judgment",
        harness: dict[str, Any] | None = None,
    ) -> tuple[Draft, DraftVersion]:
        """Сохранить проект, его версию и результат проверки.

        Отчёт передаётся готовым, а не вычисляется здесь повторно: проверка
        уже выполнена против настоящего пакета доказательств, и повторный
        прогон против урезанных данных дал бы другой, менее строгий ответ.
        """
        draft = await self.drafts.get_for_case(case_id, kind=kind)
        kind = (document_kind or "judgment").strip().lower() or "judgment"
        if draft is None:
            draft = await self.drafts.create(
                case_id,
                template_name=template_name,
                status=DraftStatus.DRAFT.value,
                document_kind=kind,
            )
        else:
            # Обновляем вид/шаблон, если дело переключилось между режимами.
            draft.template_name = template_name
            if hasattr(draft, "document_kind"):
                draft.document_kind = kind
            await self._session.flush()

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
        issues_payload = [i.to_dict() for i in report.issues]
        # Persist structured legal audit/claim proofs compatibly inside issues payload (no schema migration).
        if getattr(report, "legal_audit", None) is not None:
            issues_payload.append({"__meta__": "legal_audit", "type": "__legal_audit__", "data": report.legal_audit})
        if getattr(report, "claim_proofs", None):
            issues_payload.append({"__meta__": "claim_proofs", "type": "__claim_proofs__", "data": report.claim_proofs})
        if getattr(report, "claim_proof_summary", None):
            issues_payload.append({"__meta__": "claim_proof_summary", "type": "__claim_proof_summary__", "data": report.claim_proof_summary})
        if harness:
            issues_payload.append({"__meta__": "harness_capabilities", "type": "__harness_capabilities__", "data": harness})
        await self.drafts.add_verification(
            draft.id,
            draft_version=version.version,
            status=report_status,
            issues=issues_payload,
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
        trace_context = None
        try:
            from app.services.case_trace_context import load_case_trace_context

            trace_context = await load_case_trace_context(self._session, draft.case_id)
        except Exception:
            trace_context = None
        if not isinstance(trace_context, dict):
            trace_context = {}
        try:
            from sqlalchemy import select

            from app.models.entities import DraftGapAnswer

            rows = (
                await self._session.execute(
                    select(DraftGapAnswer).where(DraftGapAnswer.draft_id == draft_id)
                )
            ).scalars().all()
            trace_context["gap_answers"] = {row.gap_id: row.answer for row in rows}
        except Exception:
            trace_context["gap_answers"] = {}
        report = recheck_version(version, pack, trace_context=trace_context)

        issues_payload = [issue.to_dict() for issue in report.issues]
        if getattr(report, "legal_audit", None) is not None:
            issues_payload.append({"__meta__": "legal_audit", "type": "__legal_audit__", "data": report.legal_audit})
        if getattr(report, "claim_proofs", None):
            issues_payload.append({"__meta__": "claim_proofs", "type": "__claim_proofs__", "data": report.claim_proofs})
        if getattr(report, "claim_proof_summary", None):
            issues_payload.append({"__meta__": "claim_proof_summary", "type": "__claim_proof_summary__", "data": report.claim_proof_summary})
        if getattr(get_settings(), "model_harness_enabled", False):
            try:
                from app.harnesses.integration import run_harness_capabilities

                harness = await run_harness_capabilities(
                    pack=pack,
                    template=None,
                    draft=None,
                    report=report,
                    settings=get_settings(),
                    trace_context=trace_context if isinstance(trace_context, dict) else None,
                )
                if harness and harness.get("enabled"):
                    issues_payload.append(
                        {"__meta__": "harness_capabilities", "type": "__harness_capabilities__", "data": harness}
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Harness integration (reverify) failed: %s", exc)

        record = await self.drafts.add_verification(
            draft.id,
            draft_version=version.version,
            status=report.status.value,
            issues=issues_payload,
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
