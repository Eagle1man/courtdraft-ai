"""Эндпоинты проекта акта, версий и проверки (Фаза 11, п.43).

Главная особенность: формирование проекта по умолчанию выполняется
в фоне. На процессоре оно занимает две с половиной минуты, и синхронный
ответ клиенту за это время невозможен — соединение оборвалось бы по
таймауту, и пользователь счёл бы сервис зависшим.

Синхронный режим оставлен для сценариев, где клиент точно знает,
что ждать: автоматические проверки и импорт данных.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repositories import JobRepository
from app.model_routing import ModelRouter
from app.models.entities import Draft, DraftGapAnswer, DraftVersion, EvidenceItem, Verification
from app.services.case_service import db, session_scope
from app.schemas.draft import (
    DictateEditIn,
    DictateEditOut,
    DraftEditIn,
    DraftGenerateIn,
    DraftJobOut,
    DraftListOut,
    DraftOut,
    DraftStatusIn,
    DraftVersionOut,
    EditApplyIn,
    EditSpotOut,
    EditSuggestionsOut,
    EditVariantOut,
    EvidenceOut,
    EvidencePackOut,
    GapAnswerIn,
    GapOut,
    GapsOut,
    IssueOut,
    SectionOut,
    VerificationOut,
)
from app.services.draft_service import CaseNotReadyError, DraftService
from app.services.job_state import persist_job_state as job_state_persist
from app.services.jobs import Job, JobState, jobs
from app.verification.contracts import DocumentStatus, from_legacy_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["drafts"])

# Один проект за раз. При тесте на 20 студентов несколько одновременных
# генераций на 4 ядрах и 7,5 ГБ ОЗУ приводят к свопу и падению по памяти:
# модель одна, поэтому параллельные запросы всё равно ждут её освобождения.
# Очередь здесь — честная: job висит в PENDING, пока слот занят.
#
# Семафор создаётся на каждый событийный цикл: объект, привязанный к
# одному циклу, нельзя использовать в другом, а pytest поднимает новый
# цикл на каждый тест. Ключ — сам цикл, значение — семафор.
_GENERATION_SLOTS: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _generation_slots() -> asyncio.Semaphore:
    """Ограничитель одновременных генераций.

    По умолчанию один слот (`generation_max_concurrency`): модель одна,
    на 20 студентов параллельные прогоны дают своп, а не ускорение.
    Остальные задания ждут в PENDING и видят позицию и оценку времени.
    """
    loop = asyncio.get_running_loop()
    semaphore = _GENERATION_SLOTS.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(
            max(1, get_settings().generation_max_concurrency)
        )
        _GENERATION_SLOTS[loop] = semaphore
    return semaphore


async def get_session() -> AsyncIterator[AsyncSession]:
    if not db.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="База данных недоступна. Проверьте подключение в .env (DATABASE_URL).",
        )
    async with session_scope(db.factory) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


class _HasVerification(Protocol):
    """Источник данных проверки.

    Источников два: сохранённая запись Verification (обычный путь) и
    свежий отчёт VerificationReport (синхронный режим wait=true, где запись
    ещё не прочитана). Структура у них одинаковая, поэтому достаточно
    описать нужные поля, а не тащить в роутер знание о конкретных типах.

    Поля объявлены как свойства, а не как атрибуты: иначе протокол требовал
    бы точного совпадения типов, а статус у отчёта — это StrEnum, а у
    записи в базе — обычная строка.
    """

    @property
    def status(self) -> str: ...

    @property
    def issues(self) -> list[Any]: ...

    @property
    def claims_checked(self) -> int: ...

    @property
    def unsupported_claims(self) -> int: ...

    @property
    def sections_checked(self) -> int: ...


# ── Преобразование в схемы ответа ──────────────────────────────────────


def _version_out(version: DraftVersion) -> DraftVersionOut:
    return DraftVersionOut(
        id=version.id,
        draft_id=version.draft_id,
        version=version.version,
        text=version.text,
        author=version.author,
        comment=version.comment,
        citations=version.citations if isinstance(version.citations, list) else [],
        created_at=version.created_at,
    )


def _evidence_out(item: EvidenceItem) -> EvidenceOut:
    return EvidenceOut(
        id=item.id,
        category=item.category,
        content=item.content,
        source=item.source if isinstance(item.source, dict) else {},
        requires_review=item.requires_review,
    )


def _draft_out(
    draft: Draft,
    version: DraftVersion | None,
    verification: _HasVerification | None,
) -> DraftOut:
    """Собрать ответ по проекту.

    Секции и цитаты берём из citations версии: там они уже лежат
    вместе с текстом и не зависят от текущего набора доказательств,
    после того как был собран после правок.
    """
    sections: list[SectionOut] = []
    sources: list[str] = []
    text = ""

    if version is not None:
        text = version.text
        if isinstance(version.citations, list):
            for raw in version.citations:
                if not isinstance(raw, dict):
                    continue
                sections.append(
                    SectionOut(
                        key=str(raw.get("key", "")),
                        name=str(raw.get("name", "")),
                        text=str(raw.get("text", "")),
                        is_mock=bool(raw.get("is_mock", False)),
                        needs_review=bool(raw.get("needs_review", False)),
                        citations=[str(c) for c in raw.get("citations", [])],
                        latency_ms=int(raw.get("latency_ms", 0)),
                        gaps=[str(g) for g in raw.get("gaps", []) or []],
                    )
                )
                for citation in raw.get("citations", []):
                    if str(citation) not in sources:
                        sources.append(str(citation))

    verification_out: VerificationOut | None = None
    if verification is not None:
        raw_items = [
            item.to_dict() if hasattr(item, "to_dict") else item
            for item in verification.issues
        ]
        # Extract structured meta (stored compatibly in issues) or from live report attributes.
        legal_audit = getattr(verification, "legal_audit", None)
        claim_proofs = getattr(verification, "claim_proofs", None) or []
        claim_proof_summary = getattr(verification, "claim_proof_summary", None) or {}
        raw_issues: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            if item.get("__meta__") == "legal_audit" or item.get("type") == "__legal_audit__":
                if legal_audit is None:
                    legal_audit = item.get("data") or item.get("legal_audit")
                continue
            if item.get("__meta__") == "claim_proofs" or item.get("type") == "__claim_proofs__":
                if not claim_proofs:
                    claim_proofs = item.get("data") or []
                continue
            if item.get("__meta__") == "claim_proof_summary" or item.get("type") == "__claim_proof_summary__":
                if not claim_proof_summary:
                    claim_proof_summary = item.get("data") or {}
                continue
            raw_issues.append(item)

        report_version = getattr(verification, "draft_version", draft.current_version)
        stale = report_version != draft.current_version
        document_status = DocumentStatus.REVIEW_REQUIRED if stale else from_legacy_status(
            verification.status
        )
        verification_out = VerificationOut(
            status=verification.status,
            issues=[
                IssueOut(
                    type=str(item.get("type", "")),
                    severity=str(item.get("severity", "")),
                    section=str(item.get("section", "")),
                    reason=str(item.get("reason", "")),
                    text=str(item.get("text", "")),
                )
                for item in raw_issues
            ],
            claims_checked=verification.claims_checked,
            unsupported_claims=verification.unsupported_claims,
            sections_checked=verification.sections_checked,
            is_stale=stale,
            document_status=document_status.value,
            legal_audit=legal_audit,
            claim_proofs=claim_proofs if isinstance(claim_proofs, list) else [],
            claim_proof_summary=claim_proof_summary if isinstance(claim_proof_summary, dict) else {},
        )

    return DraftOut(
        id=draft.id,
        case_id=draft.case_id,
        status=draft.status,
        template_name=draft.template_name,
        document_kind=getattr(draft, "document_kind", "judgment"),
        current_version=draft.current_version,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        text=text,
        sections=sections,
        sources=sources,
        verification=verification_out,
    )

async def persist_job_state(job_id: uuid.UUID, **fields: object) -> None:
    """Совместимость: реализация в `app.services.job_state`."""
    await job_state_persist(job_id, **fields)


def _job_payload(job: Job) -> dict[str, object]:
    """Ответ о задании: состояние из реестра + живая очередь.

    Позиция и оценка остатка считаются в момент ответа: они меняются,
    и хранить их в задании значило бы отдавать устаревшие цифры.
    """
    payload = job.to_dict()
    payload.update(jobs.queue_view(job))
    return payload


async def run_generation(
    job_id: uuid.UUID,
    case_id: uuid.UUID,
    actor: str,
    cfg: Settings,
    model: str,
    template_name: str | None = None,
    document_kind: str | None = None,
    claim_data: dict[str, Any] | None = None,
) -> None:
    """Сформировать проект вне запроса.

    Вызывается и из HTTP-запроса (BackgroundTasks), и из возобновления
    после перезапуска службы (`app.services.job_recovery`) — поэтому
    функция публичная, а не `_run_generation`.

    Настройки передаются явно. Если взять их из get_settings(), фоновая
    задача пошла бы по глобальному .env и проигнорировала бы ту
    конфигурацию, с которой собрано приложение: тесты и любые сборки
    с нестандартным окружением молча получили бы не ту модель.

    Модель тоже передаётся явно: она закреплена за делом квотой и не
    должна зависеть от того, что кто-то поменял в .env между постановкой
    задачи и её запуском.
    """
    # Слот берётся до перевода job в RUNNING: пока ждём, задача честно
    # остаётся PENDING, и интерфейс показывает ожидание, а не работу.
    await jobs.update(job_id, stage="queued", progress=0)
    async with _generation_slots():
        await jobs.update(
            job_id,
            state=JobState.RUNNING,
            started_at=_now(),
            stage="generating",
            progress=5,
            kind=("claim" if document_kind == "claim" else "judgment"),
        )
        await persist_job_state(
            job_id, state=JobState.RUNNING.value, started_at=_now()
        )

        try:
            from app.llm import build_llm_provider

            llm = build_llm_provider(cfg, model=model)

            async with session_scope(db.factory) as session:
                service = DraftService(session)
                draft, version, report, _pack = await service.generate(
                    case_id,
                    provider=llm,
                    actor=actor,
                    template_name=template_name,
                    document_kind=document_kind,
                    claim_data=claim_data,
                )
                draft_id = draft.id
                version_number = version.version
                verification_status = report.status.value

            await jobs.update(job_id, stage="saving", progress=95)
            await jobs.update(
                job_id,
                state=JobState.DONE,
                finished_at=_now(),
                result_draft_id=draft_id,
                result_version=version_number,
                verification_status=verification_status,
                stage="done",
                progress=100,
            )
            await persist_job_state(
                job_id,
                state=JobState.DONE.value,
                finished_at=_now(),
                result_draft_id=draft_id,
                result_version=version_number,
                verification_status=verification_status,
            )

        except CaseNotReadyError as exc:
            # Нет материалов дела: это состояние данных, а не сбой системы.
            # Показываем причину, а не «500 Internal Server Error».
            await jobs.update(
                job_id,
                state=JobState.FAILED,
                finished_at=_now(),
                error=str(exc),
            )
            await persist_job_state(
                job_id,
                state=JobState.FAILED.value,
                finished_at=_now(),
                error=str(exc)[:400],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Формирование проекта не удалось: %s", job_id)
            message = f"{type(exc).__name__}: {exc}"[:400]
            await jobs.update(
                job_id,
                state=JobState.FAILED,
                finished_at=_now(),
                error=message,
            )
            await persist_job_state(
                job_id,
                state=JobState.FAILED.value,
                finished_at=_now(),
                error=message,
            )


def _now() -> datetime:
    return datetime.now(UTC)


# ── Эндпоинты ─────────────────────────────────────────────────────────


@router.post("/cases/{case_id}/draft", status_code=status.HTTP_202_ACCEPTED)
async def generate_draft(
    request: Request,
    case_id: uuid.UUID,
    payload: DraftGenerateIn,
    background: BackgroundTasks,
    session: SessionDep,
    wait: Annotated[bool, Query(description="Ждать готовности (только для тестов)")] = False,
) -> DraftJobOut | DraftOut:
    """POST /api/cases/{id}/draft — сформировать проект акта.

    По умолчанию возвращает 202 и job_id: формирование занимает минуты.
    С `wait=true` ждёт результата — использовать только там, где клиент
    точно готов ждать (автоматические проверки, импорт).
    """
    service = DraftService(session)

    try:
        case = await service.cases.get(case_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено"
        )

    cfg = request.app.state.settings

    # PROTECT: без действующей лицензии формирование проекта запрещено.
    # Проверка офлайн (ЭЦП правообладателя), при желании отключается
    # LICENSE_REQUIRED=false в конфигурации.
    if getattr(cfg, "license_required", True):
        from app.licensing import check_license_auto

        lic, _ = check_license_auto(cfg.license_file)
        if not lic.active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Лицензия не активна: "
                    f"{lic.detail}. Доступен только демонстрационный режим."
                ),
            )

    # Повторное нажатие не создаёт второе задание: на процессоре проект
    # пишется минуты, и пять заданий на одно дело — это очередь, в
    # которой последний студент ждёт полчаса. Возвращаем уже идущее.
    if not wait:
        active = await JobRepository(session).find_active(case_id, kind="draft")
        if active is not None:
            active_job = await jobs.get(active.id)
            view = jobs.queue_view(active_job) if active_job is not None else {}
            return DraftJobOut(
                job_id=active.id,
                case_id=case_id,
                status=active.state,
                started_at=active.created_at,
                progress=int(view.get("progress") or 0),
                stage=str(getattr(active_job, "stage", "") or ""),
                queue_position=view.get("queue_position"),
                eta_seconds=view.get("eta_seconds"),
            )

    # Модель закрепляется до постановки задачи: иначе два первых дела
    # могли бы одновременно получить сильную модель сверх квоты.
    assignment = await ModelRouter(session, cfg).assign(case_id)
    # Фоновая задача стартует после ответа и работает в своей сессии;
    # назначение должно быть уже зафиксировано, иначе она его не увидит.
    await session.commit()

    if not wait:
        job = await jobs.create(
            case_id,
            kind=("claim" if payload.document_kind == "claim" else "judgment"),
        )
        # Запись в базу до запуска фоновой задачи: иначе после
        # перезапуска службы опрос job_id не найдёт ничего.
        await JobRepository(session).create(
            job_id=job.id,
            case_id=case_id,
            state=job.state.value,
            created_at=job.created_at,
        )
        await session.commit()
        background.add_task(
            run_generation,
            job.id,
            case_id,
            payload.actor,
            cfg,
            assignment.model,
            payload.template_name,
            payload.document_kind,
            payload.claim_data,
        )
        view = jobs.queue_view(job)
        return DraftJobOut(
            job_id=job.id,
            case_id=case_id,
            status=job.state.value,
            started_at=job.created_at,
            progress=int(view.get("progress") or 0),
            stage=str(job.stage or ""),
            queue_position=view.get("queue_position"),
            eta_seconds=view.get("eta_seconds"),
        )

    try:
        # Провайдер собирается из настроек приложения, а не из глобального
        # .env: иначе синхронный режим и фоновый вели бы себя по-разному.
        from app.llm import build_llm_provider

        draft, version, report, _pack = await service.generate(
            case_id,
            provider=build_llm_provider(cfg, model=assignment.model),
            template_name=payload.template_name,
            actor=payload.actor,
            document_kind=payload.document_kind,
            claim_data=payload.claim_data,
        )
    except CaseNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    # Сохранение версии обновляет строку проекта, и серверный updated_at
    # (onupdate=func.now() в TimestampMixin) при flush «протухает»: чтение
    # протухшего атрибута вне зелёной нити падает с MissingGreenlet.
    # Явно перечитываем строку перед сериализацией — тот же приём, что
    # уже используется в PATCH /api/cases/{id}.
    await session.refresh(draft)
    return _draft_out(draft, version, report)


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, session: SessionDep) -> dict[str, object]:
    """GET /api/jobs/{id} — состояние фоновой задачи.

    Отдельный эндпоинт, а не поле в ответе на создание: клиент опрашивает
    его, пока задача не завершится. Если реестр в памяти пуст (служба
    перезапускалась), состояние читается из базы — там задача остаётся.
    """
    job = await jobs.get(job_id)
    if job is not None:
        return _job_payload(job)

    record = await JobRepository(session).get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Задача не найдена: такого задания нет ни в памяти, ни в базе.",
        )
    return {
        "job_id": str(record.id),
        "case_id": str(record.case_id),
        "state": record.state,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "error": record.error,
        "draft_id": str(record.result_draft_id) if record.result_draft_id else None,
        "version": record.result_version,
        "verification": record.verification_status,
    }


@router.get("/cases/{case_id}/draft", response_model=DraftOut | None)
async def get_draft(case_id: uuid.UUID, session: SessionDep) -> DraftOut | None:
    """GET /api/cases/{id}/draft — последняя версия проекта.

    Неизвестное дело — 404: раньше молчаливый null не давал отличить
    «проекта ещё нет» от опечатки в идентификаторе. Для существующего
    дела без проекта null остаётся — на нём держится «Проект не
    сформирован» в интерфейсе.
    """
    service = DraftService(session)
    case = await service.cases.get(case_id)
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено"
        )
    draft = await service.get_draft_for_case(case_id)
    if draft is None:
        return None

    versions = await service.list_versions(draft.id)
    version = versions[-1] if versions else None
    verification = draft.verifications[-1] if draft.verifications else None
    return _draft_out(draft, version, verification)


@router.get("/cases/{case_id}/draft/versions", response_model=DraftListOut)
async def list_versions(case_id: uuid.UUID, session: SessionDep) -> DraftListOut:
    """GET /api/cases/{id}/draft/versions — все версии проекта (п.31).

    Каждая редакция сохраняется целиком со своим автором: судья должен
    видеть документ в том виде, в каком он был принят.
    """
    service = DraftService(session)
    case = await service.cases.get(case_id)
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено"
        )
    draft = await service.get_draft_for_case(case_id)
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="По делу нет проекта. Сформируйте его командой POST /api/cases/{id}/draft.",
        )

    versions = await service.list_versions(draft.id)
    return DraftListOut(
        draft_id=draft.id,
        case_id=case_id,
        status=draft.status,
        current_version=draft.current_version,
        versions=[_version_out(v) for v in versions],
    )


@router.post(
    "/drafts/{draft_id}/versions",
    response_model=DraftVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_version(
    draft_id: uuid.UUID, payload: DraftEditIn, session: SessionDep
) -> DraftVersionOut:
    """POST /api/drafts/{id}/versions — сохранить правку человека.

    Текст приходит целиком: разметка изменений для судебного акта
    недостаточна, нужно уметь показать принятую редакцию целиком.
    """
    service = DraftService(session)
    if await service.drafts.get(draft_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Проект не найден"
        )

    try:
        version = await service.add_manual_version(
            draft_id,
            text=payload.text,
            author=payload.author,
            comment=(
                f"{payload.author_name}: {payload.comment}"
                if payload.author_name else payload.comment
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return _version_out(version)


def _evidence_sources(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    """Превратить доказательства в справочные строки для мини-агента."""
    result: list[dict[str, Any]] = []
    for item in items[:12]:
        source = item.source if isinstance(item.source, dict) else {}
        result.append(
            {
                "document": str(source.get("document") or source.get("citation") or ""),
                "quote": str(item.content or "")[:220],
            }
        )
    return result


@router.post("/drafts/{draft_id}/suggest-edits", response_model=EditSuggestionsOut)
async def suggest_edits(
    draft_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    use_llm: Annotated[bool, Query(description="Генерировать варианты моделью")] = True,
) -> EditSuggestionsOut:
    """POST /api/drafts/{id}/suggest-edits — 5 вариантов на каждое спорное место.

    Это предложение, а не правка: документ не меняется, пока человек не
    выберет вариант через ``apply-edit``. Все варианты помечаются
    ``needs_review`` — модель не имеет права ставить ``verified``.
    """
    service = DraftService(session)
    draft = await service.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    versions = await service.list_versions(draft_id)
    if not versions:
        raise HTTPException(status_code=404, detail="У проекта нет версий")
    text = versions[-1].text or ""

    try:
        from app.drafting.suggest_edits import find_gap_spots, suggest_variants

        spots = find_gap_spots(text)
        evidence = await service.list_evidence(draft.case_id)
        sources = _evidence_sources(evidence)
        kind = getattr(draft, "document_kind", "judgment") or "judgment"
        provider = None
        if use_llm:
            cfg = request.app.state.settings
            from app.llm import build_llm_provider

            provider = build_llm_provider(cfg)
        out_spots = []
        for spot in spots:
            variants = await suggest_variants(
                spot, sources, provider, kind, use_llm=bool(provider)
            )
            out_spots.append(
                EditSpotOut(
                    spot_id=spot.spot_id,
                    section=spot.section,
                    reason=spot.reason,
                    original=spot.original,
                    context=spot.context,
                    variants=[
                        EditVariantOut(index=index, text=value)
                        for index, value in enumerate(variants)
                    ],
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("suggest-edits failed for %s: %s", draft_id, type(exc).__name__)
        out_spots = []

    return EditSuggestionsOut(
        draft_id=draft.id,
        document_kind=getattr(draft, "document_kind", "judgment") or "judgment",
        spots=out_spots,
    )


@router.post("/drafts/{draft_id}/suggest-edits-stream")
async def suggest_edits_stream(
    draft_id: uuid.UUID,
    request: Request,
    session: SessionDep,
):
    """Пошаговая генерация предложений с прогрессом (SSE).

    Отдаёт событие после каждого спорного места, чтобы интерфейс показывал
    «Обработано 3 из 9» вместо молчаливого ожидания.
    """
    import asyncio

    from fastapi.responses import StreamingResponse
    from app.drafting.suggest_edits import find_gap_spots, suggest_variants

    async def generate():
        def ev(obj: dict) -> str:
            return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

        try:
            service = DraftService(session)
            draft = await service.drafts.get(draft_id)
            if draft is None:
                yield ev({"error": "Проект не найден"}); return
            versions = await service.list_versions(draft_id)
            if not versions:
                yield ev({"error": "У проекта нет версий"}); return
            text = versions[-1].text or ""

            spots = find_gap_spots(text)
            evidence = await service.list_evidence(draft.case_id)
            sources = _evidence_sources(evidence)
            kind = getattr(draft, "document_kind", "judgment") or "judgment"
            cfg = request.app.state.settings
            from app.llm import build_llm_provider
            provider = build_llm_provider(cfg)

            total = len(spots)
            yield ev({"total": total, "done": 0})
            out = []
            for i, spot in enumerate(spots, 1):
                variants = await suggest_variants(spot, sources, provider, kind, use_llm=True)
                out.append({
                    "spot_id": spot.spot_id,
                    "section": spot.section,
                    "reason": spot.reason,
                    "original": spot.original,
                    "context": spot.context,
                    "variants": [{"index": j, "text": v} for j, v in enumerate(variants)],
                })
                yield ev({"done": i, "total": total, "spot_id": spot.spot_id})
                await asyncio.sleep(0)

            yield ev({"done": total, "total": total, "spots": out, "finished": True})
        except Exception as exc:  # noqa: BLE001
            logger.warning("suggest-edits-stream failed: %s", type(exc).__name__)
            yield ev({"error": str(exc)})

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/drafts/{draft_id}/export-docx")
async def export_draft_docx(
    draft_id: uuid.UUID,
    session: SessionDep,
) -> Response:
    """Скачать проект в DOCX, оформленном по ГОСТ Р (готов к подаче)."""
    from app.services.export_docx import export_docx

    service = DraftService(session)
    draft = await service.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    versions = await service.list_versions(draft_id)
    if not versions:
        raise HTTPException(status_code=404, detail="У проекта нет версий")
    latest = versions[-1]
    text = latest.text or ""

    # Разбивка на разделы (если сохранилась) — тогда DOCX будет структурирован.
    sections = getattr(latest, "sections", None)
    sec_list = sections if isinstance(sections, list) else None

    case = await service.cases.get(draft.case_id)
    number = getattr(case, "case_number", "") or ""
    court = getattr(case, "court_name", "") or ""
    kind = getattr(draft, "document_kind", "judgment") or "judgment"
    title = "Исковое заявление" if kind == "claim" else "Решение суда"

    # Гейт экспорта: блокирующие замечания или устаревший отчёт → 409
    from app.services.export_gate import ExportBlockedError, assert_export_allowed
    from app.textnorm import broken_numbers

    verification = draft.verifications[-1] if draft.verifications else None
    raw_issues = verification.issues if verification else []
    issues = raw_issues if isinstance(raw_issues, list) else []
    p0 = sum(
        1
        for item in issues
        if isinstance(item, dict) and str(item.get("severity")) == "CRITICAL"
    )
    p0 += len(broken_numbers(text))
    stale = bool(verification and verification.draft_version != latest.version)
    try:
        assert_export_allowed(p0_count=p0, is_stale=stale, mode="final")
    except ExportBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    data = export_docx(
        title=title, text=text, case_number=number, court=court,
        document_kind=kind, sections=sec_list,
    )
    # HTTP-заголовки — только ASCII/latin-1: кириллица в filename ломает Response.
    # Человекочитаемое название остаётся внутри самого DOCX (заголовок).
    ascii_title = "resolution" if kind == "judgment" else "claim"
    ascii_number = re.sub(r"[^A-Za-z0-9_-]+", "_", number) or "case"
    fname = f"{ascii_title}_{ascii_number}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/drafts/{draft_id}/dictate-edit", response_model=DictateEditOut)
async def dictate_edit(
    draft_id: uuid.UUID,
    payload: DictateEditIn,
    request: Request,
    session: SessionDep,
) -> DictateEditOut:
    """Голосовая команда «как секретарю» → предложение правки по разделу.

    Ничего не применяется: модель только предлагает формулировку, человек
    видит её и решает — принять или нет. Работает с уже подключённой LLM
    (в том числе офлайн-моделью через Ollama).
    """
    service = DraftService(session)
    draft = await service.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    versions = await service.list_versions(draft_id)
    if not versions:
        raise HTTPException(status_code=404, detail="У проекта нет версий")
    text = versions[-1].text or ""
    section = payload.section.strip()

    # Найдём текст выбранного раздела по его названию прямо в тексте.
    original = ""
    if section:
        pos = text.lower().find(section.lower())
        if pos >= 0:
            # До следующего заголовка раздела (строки, начинающейся с цифры и точки)
            import re
            rest = text[pos:]
            m = re.search(r"\n\s*\d+[\.\)]\s+[А-ЯЁA-Z]", rest[5:])
            original = rest[: m.start() + 5] if m else rest[:1200]
    if not original:
        original = text[:900]

    try:
        from app.llm import build_llm_provider
        from app.llm.base import LLMMessage, LLMRequest, LLMRole

        cfg = request.app.state.settings
        provider = build_llm_provider(cfg)
        system = (
            "Ты помощник судьи. Человек продиктовал голосом, что поправить "
            "в проекте документа. Предложи concrete правку. "
            "НЕ выдумывай суммы, даты, нормы и ФИО — если их нет в исходнике, "
            "оставь пометку [требует проверки]. "
            "Не пиши как суд. Ответь ТОЛЬКО в формате: "
            "ПРАВКА: <как именно звучит исправленный фрагмент>\n"
            "ПОЯСНЕНИЕ: <одно предложение, что и почему изменено>"
        )
        prompt = (
            f"Раздел: {section or 'не указан'}\n"
            f"Текст раздела:\n{original[:1200]}\n\n"
            f"Голосовая команда: {payload.command}\n"
        )
        response = await provider.generate(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=system),
                    LLMMessage(role="user", content=prompt),
                ],
                role=LLMRole.GENERATION,
                temperature=0.1,
                max_tokens=500,
            )
        )
        raw = (response.text or "").strip()
        proposed, explanation = "", ""
        for line in raw.splitlines():
            low = line.lower()
            if low.startswith("правка:"):
                proposed = line.split(":", 1)[1].strip()
            elif low.startswith("пояснение:"):
                explanation = line.split(":", 1)[1].strip()
        if not proposed:
            proposed = raw[:600]
        if not proposed:
            raise ValueError("модель вернула пустой ответ")
        return DictateEditOut(
            draft_id=draft.id,
            section=section,
            command=payload.command,
            original=original[:400],
            proposed=proposed,
            explanation=explanation,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dictate-edit failed for %s: %s", draft_id, type(exc).__name__)
        return DictateEditOut(
            draft_id=draft.id,
            section=section,
            command=payload.command,
            original=original[:400],
            proposed=payload.command,
            explanation="Не удалось получить предложение модели — проверьте подключение языковой модели.",
            ok=False,
        )


@router.get("/drafts/{draft_id}/gaps", response_model=GapsOut)
async def get_gaps(draft_id: uuid.UUID, session: SessionDep) -> GapsOut:
    """Целевые вопросы по пробелам, которые нельзя вывести из текста."""
    service = DraftService(session)
    draft = await service.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    versions = await service.list_versions(draft_id)
    text = versions[-1].text or "" if versions else ""
    verification = (
        await session.execute(
            select(Verification)
            .where(Verification.draft_id == draft_id)
            .order_by(Verification.created_at.desc())
        )
    ).scalars().first()
    try:
        raw_issues = verification.issues if verification else []
        issues = json.loads(raw_issues) if isinstance(raw_issues, str) else list(raw_issues or [])
    except (TypeError, ValueError):
        issues = []
    from app.verification.gaps import find_gaps

    gaps = find_gaps(issues, text)
    answers = (
        await session.execute(
            select(DraftGapAnswer).where(DraftGapAnswer.draft_id == draft_id)
        )
    ).scalars().all()
    answer_map = {row.gap_id: row.answer for row in answers}
    out = [
        GapOut(
            gap_id=gap.gap_id,
            kind=gap.kind,
            question=gap.question,
            reason=gap.reason,
            options=gap.options,
            blocking=gap.blocking,
            source_hint=gap.source_hint,
            answered=gap.gap_id in answer_map,
            answer=answer_map.get(gap.gap_id, ""),
        )
        for gap in gaps
    ]
    blocked = any(gap.blocking and not gap.answered for gap in out)
    return GapsOut(draft_id=draft.id, gaps=out, blocked=blocked)


@router.post("/drafts/{draft_id}/gaps/answer", response_model=GapsOut)
async def answer_gap(
    draft_id: uuid.UUID, payload: GapAnswerIn, session: SessionDep
) -> GapsOut:
    """Сохранить ответ судьи как факт «ввод судьи» и вернуть обновлённые вопросы."""
    draft = await DraftService(session).drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    row = (
        await session.execute(
            select(DraftGapAnswer).where(
                DraftGapAnswer.draft_id == draft_id,
                DraftGapAnswer.gap_id == payload.gap_id,
            )
        )
    ).scalars().first()
    if row is None:
        now = datetime.now(UTC)
        row = DraftGapAnswer(
            draft_id=draft_id,
            gap_id=payload.gap_id,
            answer=payload.answer,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.answer = payload.answer
    await session.commit()
    return await get_gaps(draft_id, session)


@router.post("/drafts/{draft_id}/apply-edit", response_model=DraftVersionOut)
async def apply_edit(
    draft_id: uuid.UUID, payload: EditApplyIn, session: SessionDep
) -> DraftVersionOut:
    """POST /api/drafts/{id}/apply-edit — принять вариант человеком.

    Сохраняется новая версия с автором-человеком. Модель сама никогда не
    меняет текст: succeeds только после явного выбора варианта или своего
    текста от юриста.
    """
    if sum(x is not None for x in (payload.variant_text, payload.custom_text)) != 1:
        raise HTTPException(
            status_code=422,
            detail="Укажите либо variant_text (выбранный вариант), либо custom_text — не оба",
        )

    service = DraftService(session)
    draft = await service.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    versions = await service.list_versions(draft_id)
    if not versions:
        raise HTTPException(status_code=404, detail="У проекта нет версий")

    from app.drafting.suggest_edits import apply_variant, find_gap_spots

    text = versions[-1].text or ""
    spots = find_gap_spots(text)
    spot = next((s for s in spots if s.spot_id == payload.spot_id), None)
    if spot is None:
        raise HTTPException(status_code=404, detail="Проблемное место не найдено")

    chosen = payload.custom_text or payload.variant_text
    if not chosen:
        raise HTTPException(status_code=422, detail="Не передан текст варианта")
    try:
        new_text = apply_variant(text, spot, chosen)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    comment = payload.comment or f"Принят вариант мини-агента для {payload.spot_id}"
    # ФИО внёсшего правку сохраняем в комментарии: в истории версий видно,
    # кто именно принял решение, а не только роль «судья».
    if payload.author_name:
        comment = f"{payload.author_name}: {comment}"
    version = await service.add_manual_version(
        draft_id,
        text=new_text,
        author=payload.author,
        comment=comment,
    )
    return _version_out(version)


@router.get("/cases/{case_id}/evidence", response_model=EvidencePackOut)
async def get_evidence(
    case_id: uuid.UUID,
    session: SessionDep,
    category: Annotated[str | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> EvidencePackOut:
    """GET /api/cases/{id}/evidence — пакет доказательств.

    Отдаётся вместе с проектом, чтобы интерфейс мог показать, из каких
    документов построен каждый раздел.
    """
    service = DraftService(session)
    case = await service.cases.get(case_id)
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено"
        )
    items = await service.list_evidence(case_id, category=category, limit=limit)
    return EvidencePackOut(
        case_id=case_id,
        facts=len(items),
        items=[_evidence_out(i) for i in items],
    )


@router.patch("/drafts/{draft_id}/status", response_model=DraftOut)
async def set_status(
    draft_id: uuid.UUID, payload: DraftStatusIn, session: SessionDep
) -> DraftOut:
    """PATCH /api/drafts/{id}/status — сменить статус проекта."""
    service = DraftService(session)
    try:
        draft = await service.set_status(draft_id, payload.status.value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Проект не найден"
        )

    versions = await service.list_versions(draft_id)
    version = versions[-1] if versions else None
    verification = draft.verifications[-1] if draft.verifications else None
    # Смена статуса — UPDATE строки проекта: серверный updated_at
    # протухает при flush, и его чтение вне зелёной нити роняет запрос
    # с MissingGreenlet. Перечитываем после чтения отношений (versions,
    # verifications), чтобы refresh не вливал их заново без необходимости.
    await session.refresh(draft)
    return _draft_out(draft, version, verification)


@router.post("/drafts/{draft_id}/reverify", response_model=VerificationOut)
async def reverify_draft(
    draft_id: uuid.UUID, session: SessionDep
) -> VerificationOut:
    """POST /api/drafts/{id}/reverify — проверить сохранённый текст заново.

    Нужен после ручной правки: старый отчёт к новой версии не относится.
    Статус здесь не переносится и не наследуется — он считается из
    свежего отчёта, и «исправлено» без проверки не появляется.
    """
    service = DraftService(session)
    try:
        draft, version, record, _report = await service.reverify(draft_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Проект не найден"
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    verification = _draft_out(draft, version, record).verification
    if verification is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Отчёт проверки не сформирован",
        )
    return verification


@router.get("/drafts/{draft_id}/claims")
async def get_draft_claims(
    draft_id: uuid.UUID, session: SessionDep
) -> dict[str, object]:
    """GET /api/drafts/{id}/claims — утверждения проекта с источниками.

    Судья видит не только текст, но и чем подтверждено каждое
    утверждение. Утверждение без источника получает явный статус, а не
    выглядит подтверждённым. Ответ аддитивный: остальные эндпоинты не
    меняются.
    """
    from decimal import Decimal

    from app.drafting.requirements import extract_requirements
    from app.verification.claims import (
        build_claims,
        check_completeness,
        summarize_claims,
    )
    from app.verification.contracts import Claim, text_checksum

    service = DraftService(session)
    draft = await service.drafts.get(draft_id)
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Проект не найден"
        )

    versions = await service.list_versions(draft_id)
    if not versions:
        return {
            "draft_id": str(draft_id),
            "version": 0,
            "text_sha256": "",
            "summary": summarize_claims([]),
            "claims": [],
        }

    version = versions[-1]
    documents = await service.documents.list_for_case(draft.case_id)
    texts = {document.filename: document.text for document in documents}

    # Утверждения строятся по тексту раздела, а не по всему проекту:
    # судья читает их рядом с абзацем, к которому они относятся. Разбивка
    # уже сохранена в citations версии и не зависит от текущего пакета
    # доказательств, который мог быть пересобран после правки.
    claim_sections: list[tuple[str, str]] = []
    if isinstance(version.citations, list):
        for position, raw in enumerate(version.citations, start=1):
            if not isinstance(raw, dict):
                continue
            section_text = str(raw.get("text") or "")
            if not section_text.strip():
                continue
            key = str(raw.get("key") or raw.get("name") or f"section{position}")
            claim_sections.append((key, section_text))
    if not claim_sections:
        # Версия без разбивки (старые записи): весь текст одним блоком.
        claim_sections = [("draft", version.text)]

    claims: list[Claim] = []
    for key, section_text in claim_sections:
        claims.extend(build_claims(section_text, texts, section=key))

    # Полнота: требования из материалов, не отражённые в проекте.
    pack = await service.build_pack(draft.case_id)
    money, _others, _unrecognised = extract_requirements(pack)
    amounts = [Decimal(str(claim.amount)) for claim in money if claim.amount is not None]
    missing = check_completeness(version.text, amounts)

    all_claims = claims + missing
    return {
        "draft_id": str(draft_id),
        "version": version.version,
        "text_sha256": text_checksum(version.text),
        "summary": summarize_claims(all_claims),
        "claims": [claim.model_dump(mode="json") for claim in all_claims],
    }
