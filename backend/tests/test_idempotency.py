"""Идемпотентность: повторные прогоны и правки не накапливают мусор."""
from __future__ import annotations

import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "case_2-101-2025"


def test_header_never_repeats() -> None:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    model = ClaimModel.from_passport(passport)
    text = render_claim(model).text
    assert text.count("судебного участка № 5") == 1


def test_slot_edit_replaces_not_appends() -> None:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    model = ClaimModel.from_passport(passport)
    for value in ("Районный суд г. Москвы", "Мировому судье судебного участка № 5 Центрального района г. Москвы"):
        model = model.with_court(value)
        text = render_claim(model).text
        assert text.count(value) == 1
        assert "подлежат проверке)" not in text
