"""Паспорт дела и журнал расхождений: чтение, запись, проверка."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status

from app.api.routes.cases import SessionDep
from app.db.repositories import (
    CasePassportRepository,
    CaseRepository,
    DeviationRepository,
    DocumentRepository,
)
from app.ingestion.passport import CasePassport
from app.models.entities import Case
from app.passport_ai import reader
from app.passport_ai.llmclient import make_llm
from app.passport_ai.models import DocIntake, Fragment
from app.passport_ai.service import PassportService
from app.schemas.passport import (
    DeviationOut,
    PassportAIAnalysisOut,
    PassportAIStatusOut,
    PassportFieldEditIn,
    PassportIn,
    PassportIssueOut,
    PassportOut,
    PassportValidationOut,
)

router = APIRouter(prefix="/api/cases", tags=["passport"])


def _issues(raw: list[Any]) -> list[PassportIssueOut]:
    out: list[PassportIssueOut] = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(
                PassportIssueOut(
                    code=str(item.get("code", "")),
                    severity=str(item.get("severity", "P1")),
                    reason=str(item.get("reason", "")),
                )
            )
    return out


@router.get("/{case_id}/passport", response_model=PassportOut | None)
async def get_passport(case_id: uuid.UUID, session: SessionDep) -> PassportOut | None:
    if await CaseRepository(session).get(case_id) is None:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    record = await CasePassportRepository(session).get_for_case(case_id)
    if record is None:
        return None
    return PassportOut(
        case_id=case_id,
        schema_id=record.schema_id,
        mode=record.mode,
        status=record.status,
        source=record.source,
        payload=record.payload,
        issues=_issues(record.issues),
        updated_at=record.updated_at,
    )


@router.put("/{case_id}/passport", response_model=PassportOut)
async def put_passport(
    case_id: uuid.UUID, payload: PassportIn, session: SessionDep
) -> PassportOut:
    if await CaseRepository(session).get(case_id) is None:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    try:
        passport = CasePassport.model_validate(payload.payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Паспорт не прошёл проверку схемы: {exc}",
        ) from exc
    issues = passport.validate_invariants()
    record = await CasePassportRepository(session).upsert(
        case_id,
        schema_id=passport.schema_id,
        mode=passport.mode,
        status=passport.status,
        payload=passport.as_dict(),
        issues=[i.model_dump() for i in issues],
        source=payload.source,
    )
    await session.commit()
    return PassportOut(
        case_id=case_id,
        schema_id=record.schema_id,
        mode=record.mode,
        status=record.status,
        source=record.source,
        payload=record.payload,
        issues=_issues(record.issues),
        updated_at=record.updated_at,
    )


@router.post("/{case_id}/passport/validate", response_model=PassportValidationOut)
async def validate_passport(
    case_id: uuid.UUID, payload: PassportIn, session: SessionDep
) -> PassportValidationOut:
    try:
        passport = CasePassport.model_validate(payload.payload)
    except Exception as exc:  # noqa: BLE001
        return PassportValidationOut(
            status="invalid",
            issues=[PassportIssueOut(code="SCHEMA", severity="P0", reason=str(exc)[:300])],
        )
    issues = passport.validate_invariants()
    return PassportValidationOut(
        status=passport.status,
        issues=[PassportIssueOut(**i.model_dump()) for i in issues],
    )


@router.get("/{case_id}/deviations", response_model=list[DeviationOut])
async def list_deviations(case_id: uuid.UUID, session: SessionDep) -> list[DeviationOut]:
    rows = await DeviationRepository(session).list_for_case(case_id)
    return [
        DeviationOut(code=row.code, severity=row.severity, payload=row.payload)
        for row in rows
    ]


# ── Паспорт-ИИ: дополнительный контур, старые /passport не заменяет ────────

_AI_ROOT = Path(__file__).resolve().parents[4]


def _ai_service(request: Request) -> PassportService:
    cfg = getattr(request.app.state, "settings", None)
    if cfg is None:
        from app.config import get_settings

        cfg = get_settings()
    import json

    contract_path = _AI_ROOT / "artifacts" / "passport" / "passport_contract.json"
    field_map_path = _AI_ROOT / "artifacts" / "passport" / "field_map.json"
    if not contract_path.exists() or not field_map_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Контракт паспорта не обнаружен. "
                "Сначала: python scripts/passport_audit.py"
            ),
        )
    contract = json.loads(contract_path.read_text("utf-8"))
    field_map = json.loads(field_map_path.read_text("utf-8"))
    return PassportService(Path(cfg.storage_dir), make_llm(), contract, field_map)


async def _ensure_ai_case(
    case_id: uuid.UUID,
    session: SessionDep,
    student_code: str | None,
) -> Case:
    case = await CaseRepository(session).get(case_id)
    if case is None or case.is_archived:
        raise HTTPException(status_code=404, detail="Дело не найдено")
    if student_code and case.student_code and case.student_code != student_code:
        raise HTTPException(status_code=403, detail="Доступ к делу запрещён")
    return case


async def _ai_documents(
    case_id: uuid.UUID, session: SessionDep
) -> tuple[list[DocIntake], dict[str, list[Fragment]]]:
    documents = await DocumentRepository(session).list_for_case(case_id)
    return reader.from_db_documents(documents)


@router.post(
    "/{case_id}/passport-ai/analyze",
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyze_passport_ai(
    case_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    student_code: str | None = Query(default=None),
) -> dict[str, Any]:
    await _ensure_ai_case(case_id, session, student_code)
    documents, fragments = await _ai_documents(case_id, session)
    if not documents:
        raise HTTPException(status_code=409, detail="В деле нет документов")
    service = _ai_service(request)

    async def job() -> None:
        await service.run(
            str(case_id),
            existing_docs=documents,
            fragments_by_doc=fragments,
        )

    background.add_task(job)
    return {"ok": True, "case_id": str(case_id), "documents": len(documents)}


@router.get(
    "/{case_id}/passport-ai/status",
    response_model=PassportAIStatusOut,
)
async def status_passport_ai(
    case_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    student_code: str | None = Query(default=None),
) -> PassportAIStatusOut:
    await _ensure_ai_case(case_id, session, student_code)
    return PassportAIStatusOut(**_ai_service(request).status(str(case_id)))


@router.get(
    "/{case_id}/passport-ai/analysis",
    response_model=PassportAIAnalysisOut,
)
async def analysis_passport_ai(
    case_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    student_code: str | None = Query(default=None),
) -> PassportAIAnalysisOut:
    await _ensure_ai_case(case_id, session, student_code)
    return PassportAIAnalysisOut(**_ai_service(request).analysis(str(case_id)))


@router.patch("/{case_id}/passport-ai/fields/{role}")
async def edit_passport_ai_field(
    case_id: uuid.UUID,
    role: str,
    payload: PassportFieldEditIn,
    request: Request,
    session: SessionDep,
    student_code: str | None = Query(default=None),
) -> dict[str, Any]:
    await _ensure_ai_case(case_id, session, student_code)
    try:
        return _ai_service(request).update_field(
            str(case_id), role, payload.value, payload.action
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{case_id}/passport-ai/confirm")
async def confirm_passport_ai(
    case_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    student_code: str | None = Query(default=None),
) -> dict[str, Any]:
    await _ensure_ai_case(case_id, session, student_code)
    service = _ai_service(request)
    result = service.confirm(str(case_id))
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])

    try:
        passport = CasePassport.model_validate(result["draft"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=409, detail=f"Паспорт не прошёл схему: {exc}"
        ) from exc
    issues = passport.validate_invariants()
    blocking = [issue for issue in issues if issue.severity == "P0"]
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=f"Паспорт не прошёл проверку: {blocking[0].reason}",
        )

    repository = CasePassportRepository(session)
    previous = await repository.get_for_case(case_id)
    backup_path = None
    if previous is not None:
        import json
        from datetime import datetime

        backup_dir = service.case_dir(str(case_id)) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / (
            f"passport-{datetime.now():%Y%m%d-%H%M%S}.json"
        )
        backup_path.write_text(
            json.dumps(previous.payload, ensure_ascii=False, indent=2),
            "utf-8",
        )

    await repository.upsert(
        case_id,
        schema_id=passport.schema_id,
        mode=passport.mode,
        status=passport.status,
        payload=passport.as_dict(),
        issues=[issue.model_dump() for issue in issues],
        source="passport_ai",
    )
    await session.commit()
    service.mark_confirmed(str(case_id))
    return {
        "ok": True,
        "backup": str(backup_path) if backup_path else None,
        "status": passport.status,
    }


@router.post(
    "/{case_id}/passport-ai/documents/{document_id}/reprocess",
    status_code=status.HTTP_202_ACCEPTED,
)
async def reprocess_passport_ai_document(
    case_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    student_code: str | None = Query(default=None),
) -> dict[str, Any]:
    await _ensure_ai_case(case_id, session, student_code)
    document = await DocumentRepository(session).get(document_id)
    if document is None or document.case_id != case_id:
        raise HTTPException(status_code=404, detail="Документ не найден")
    documents, fragments = await _ai_documents(case_id, session)
    service = _ai_service(request)

    async def job() -> None:
        await service.run(
            str(case_id),
            existing_docs=documents,
            fragments_by_doc=fragments,
            force_reprocess_ids={str(document_id)},
        )

    background.add_task(job)
    return {"ok": True, "document_id": str(document_id)}
