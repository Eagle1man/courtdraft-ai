from __future__ import annotations

import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "case_2-101-2025"


def _payload() -> dict:
    return json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))


def test_passport_parses_and_is_ok() -> None:
    from app.ingestion.passport import CasePassport

    passport = CasePassport.model_validate(_payload())
    assert passport.status == "ok"
    assert passport.money_kopecks.total == 10316329


def test_broken_arithmetic_is_flagged() -> None:
    from app.ingestion.passport import CasePassport

    data = _payload()
    data["money_kopecks"]["total"] = 1
    passport = CasePassport.model_validate(data)
    codes = {i.code for i in passport.validate_invariants()}
    assert "И-4" in codes
    assert passport.status == "needs_review"


def test_detect_in_documents() -> None:
    from app.ingestion.passport import find_passport_in_documents

    docs = {"01-Карточка.md": "текст", "00-ПАСПОРТ-ДЕЛА.json": json.dumps(_payload())}
    assert find_passport_in_documents(docs) is not None


def test_claim_model_from_passport() -> None:
    from app.drafting.claim_model import ClaimModel
    from app.ingestion.passport import CasePassport

    model = ClaimModel.from_passport(CasePassport.model_validate(_payload()).as_dict())
    assert len(model.demands) == 3
    assert model.attachments_sheets == 9