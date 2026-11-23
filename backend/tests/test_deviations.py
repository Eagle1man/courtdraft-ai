from __future__ import annotations

import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "case_2-101-2025"


def test_expected_deviations() -> None:
    from app.drafting.claim_model import ClaimModel
    from app.ingestion.passport import CasePassport
    from app.verification.deviations import build_deviations

    passport = CasePassport.model_validate(
        json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    )
    rows = build_deviations(ClaimModel.from_passport(passport.as_dict()), passport)
    codes = {r["code"] for r in rows}
    assert {"X09", "FEE", "INT_CLOSURE", "INT_PERIOD"} <= codes
    fee = next(r for r in rows if r["code"] == "FEE")
    assert fee["by_law_kopecks"] == 400790


def test_fee_by_filing_date() -> None:
    from datetime import date

    from app.fees import claim_fee_kopecks

    assert claim_fee_kopecks(10026329, date(2025, 1, 22)) == 400790
    assert claim_fee_kopecks(9000000, date(2024, 8, 1)) == 290000