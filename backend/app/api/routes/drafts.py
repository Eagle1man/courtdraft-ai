"""Эндпоинты проекта акта, версий и проверки (Фаза 11, п.43).

Главная особенность: формирование проекта по умолчанию выполняется
в фоне. На процессоре оно занимает две с половиной минуты, и синхронный
ответ клиенту за это время невозможен — соединение оборвалось бы по
таймауту, и пользователь счёл бы сервис зависшим.

Синхронный режим оставлен для сценариев, где клиент точно знает,
что ждать: автоматические проверки и импорт данных.
"""

from __future__ import annotations

import logging
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
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.entities import Draft, DraftVersion, EvidenceItem
from app.schemas.draft import (
    DraftEditIn,
    DraftGenerateIn,
    DraftJobOut,
    DraftListOut,
    DraftOut,
    DraftStatusIn,
    DraftVersionOut,
    EvidenceOut,
    EvidencePackOut,
    IssueOut,
    SectionOut,
    VerificationOut,
)
from app.services.case_service import db, session_scope
from app.services.draft_service import CaseNotReadyError, DraftService
from app.services.jobs import JobState, jobs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["drafts"])


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

    Разбивка по разделам берётся из citations версии: там она уже сохранена
    вместе с текстом и не зависит от текущего состояния пакета
    доказательств, который мог быть пересобран после правки.
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
                    )
                )
                for citation in raw.get("citations", []):
                    if str(citation) not in sources:
                        sources.append(str(citation))

    verification_out: VerificationOut | None = None
    if verification is not None:
        # Свежий отчёт (синхронный wait=true) держит объекты Issue
        # с to_dict(), запись Verification из базы — уже словари JSON.
        # Без нормализации объекты отсекались проверкой isinstance(dict),
        # и ответ синхронной генерации отвечал «0 замечаний», пока в
        # сохранённой базе их было несколько (проверено на REAL-01: 0 vs 5).
        raw_issues = [
            item.to_dict() if hasattr(item, "to_dict") else item
            for item in verification.issues
        ]
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
                if isinstance(item, dict)
            ],
            claims_checked=verification.claims_checked,
            unsupported_claims=verification.unsupported_claims,
            sections_checked=verification.sections_checked,
        )

    return DraftOut(
        id=draft.id,
        case_id=draft.case_id,
        status=draft.status,
        template_name=draft.template_name,
        current_version=draft.current_version,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        text=text,
        sections=sections,
        sources=sources,
        verification=verification_out,
    )


# ── Фоновая задача ────────────────────────────────────────────────────


async def _run_generation(
    job_id: uuid.UUID,
    case_id: uuid.UUID,
    actor: str,
    cfg: Settings,
) -> None:
    """Сформировать проект вне запроса.

    Настройки передаются явно. Если взять их из get_settings(), фоновая
    задача пошла бы по глобальному .env и проигнорировала бы ту
    конфигурацию, с которой собрано приложение: тесты и любые сборки
    с нестандартным окружением молча получили бы не ту модель.
    """
    await jobs.update(job_id, state=JobState.RUNNING, started_at=_now())

    try:
        from app.llm import build_llm_provider

        llm = build_llm_provider(cfg)

        async with session_scope(db.factory) as session:
            service = DraftService(session)
            draft, version, report, _pack = await service.generate(
                case_id, provider=llm, actor=actor
            )
            draft_id = draft.id
            version_number = version.version
            verification_status = report.status.value

        await jobs.update(
            job_id,
            state=JobState.DONE,
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("Формирование проекта не удалось: %s", job_id)
        await jobs.update(
            job_id,
            state=JobState.FAILED,
            finished_at=_now(),
            error=f"{type(exc).__name__}: {exc}"[:400],
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

    if not wait:
        job = await jobs.create(case_id)
        cfg = request.app.state.settings
        background.add_task(_run_generation, job.id, case_id, payload.actor, cfg)
        return DraftJobOut(
            job_id=job.id,
            case_id=case_id,
            status=job.state.value,
            started_at=job.created_at,
        )

    try:
        # Провайдер собирается из настроек приложения, а не из глобального
        # .env: иначе синхронный режим и фоновый вели бы себя по-разному.
        from app.llm import build_llm_provider

        cfg = request.app.state.settings
        draft, version, report, _pack = await service.generate(
            case_id,
            provider=build_llm_provider(cfg),
            template_name=payload.template_name,
            actor=payload.actor,
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
async def get_job(job_id: uuid.UUID) -> dict[str, object]:
    """GET /api/jobs/{id} — состояние фоновой задачи.

    Отдельный эндпоинт, а не поле в ответе на создание: клиент опрашивает
    его, пока задача не завершится.
    """
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Задача не найдена. Задачи хранятся в памяти процесса и "
                "теряются при перезапуске сервера."
            ),
        )
    return job.to_dict()


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
            comment=payload.comment,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

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
