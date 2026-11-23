"""Золотой тест: рендер иска из паспорта совпадает с эталоном."""
from __future__ import annotations

import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "case_2-101-2025"


def _norm(text: str) -> str:
    return "\n".join(ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")).strip()


def test_golden_render() -> None:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    rendered = render_claim(ClaimModel.from_passport(passport)).text
    golden = (FIX / "expected_claim.txt").read_text(encoding="utf-8")
    assert _norm(rendered) == _norm(golden)


def test_render_is_pure() -> None:
    """Три прогона дают идентичный текст; рендер не ходит в БД и LLM."""
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    model = ClaimModel.from_passport(passport)
    runs = [render_claim(model).text for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_invariants_in_kopecks() -> None:
    from app.drafting.claim_model import ClaimModel

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    model = ClaimModel.from_passport(passport)
    money = passport["money_kopecks"]
    assert money["principal"] == money["loan"] - money["repaid"]
    assert money["price"] == money["principal"] + money["interest"]
    assert money["total"] == money["price"] + money["fee_paid"]
    assert sum(p["amount_kopecks"] for p in passport["interest"]["periods"]) == money["interest"]
    assert sum(p["days"] for p in passport["interest"]["periods"]) == passport["interest"]["days"]
    assert len(model.demands) == 3
