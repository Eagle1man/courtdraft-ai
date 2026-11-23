"""Паспорт дела и журнал расхождений: чтение, запись, проверка."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.routes.cases import SessionDep
from app.db.repositories import CasePassportRepository, CaseRepository, DeviationRepository
from app.ingestion.passport import CasePassport
from app.schemas.passport import (
    DeviationOut,
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
