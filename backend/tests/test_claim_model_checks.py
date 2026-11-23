from __future__ import annotations

import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "case_2-101-2025"


def _render() -> tuple:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim
    from app.ingestion.passport import CasePassport

    passport = CasePassport.model_validate(
        json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    )
    model = ClaimModel.from_passport(passport.as_dict())
    return model, render_claim(model).text, passport


def test_golden_has_no_p0() -> None:
    from app.verification.claim_model_checks import REQUIRED_CHECKS, verify_claim_model

    model, text, passport = _render()
    result = verify_claim_model(model, text, passport=passport)
    assert result.p0_count == 0, [f.reason for f in result.findings if f.severity == "P0"]
    assert set(result.executed) == set(REQUIRED_CHECKS)


def test_price_29_is_blocked() -> None:
    from app.verification.claim_model_checks import verify_claim_model
    from app.verification.engine import VerificationStatus

    model, text, passport = _render()
    broken = text.replace("100 263 (Сто тысяч двести шестьдесят три) рубля 29 копеек", "29")
    result = verify_claim_model(model, broken, passport=passport)
    assert result.p0_count > 0
    assert result.to_report().status is VerificationStatus.BLOCKED


def test_broken_number_is_p0() -> None:
    from app.verification.claim_model_checks import verify_claim_model

    model, text, passport = _render()
    result = verify_claim_model(model, text.replace("90 000,00", "9 0 000,00"), passport=passport)
    assert any(f.code == "X12" and f.severity == "P0" for f in result.findings)


def test_final_mode_blocks_missing_addresses() -> None:
    from app.verification.claim_model_checks import verify_claim_model

    model, text, passport = _render()
    result = verify_claim_model(model, text, passport=passport, mode="final")
    assert any(f.code == "X13" and f.severity == "P0" for f in result.findings)