"""Проверки резолютивной части на датасете фикстур: defect — найден, clean — пусто."""
from __future__ import annotations

import pytest

from app.verification.resol_checks import check_resolution

from .resol_corpus import Fixture, load

CHECKS, FIXTURES = load()
TEXT_FIXTURES = [f for f in FIXTURES if f.kind in {"defect", "clean"}]
CLEAN = [f.id for f in FIXTURES if f.kind == "clean"]


def _fired(fixture: Fixture) -> set[str]:
    return {d.id for d in check_resolution(fixture.text, fixture.meta)}


@pytest.mark.parametrize("fixture", TEXT_FIXTURES, ids=[f.id for f in TEXT_FIXTURES])
def test_fixture(fixture: Fixture) -> None:
    fired = _fired(fixture)
    if fixture.kind == "clean":
        assert fired == set(), f"{fixture.id}: ложные срабатывания {sorted(fired)} (clean)"
        return
    missing = fixture.expected - fired
    assert not missing, f"{fixture.id}: не найдено {sorted(missing)}"
    unexpected = fired - fixture.expected - fixture.allow
    assert not unexpected, f"{fixture.id}: лишние срабатывания {sorted(unexpected)}"


def test_every_check_has_defect_fixture() -> None:
    declared = {c["id"] for c in CHECKS["checks"]}
    covered = {e for f in FIXTURES if f.kind == "defect" for e in f.expected}
    assert declared == covered, f"без фикстуры: {sorted(declared - covered)}; лишние: {sorted(covered - declared)}"


def test_disputed_report(capsys: pytest.CaptureFixture[str]) -> None:
    lines = []
    for f in FIXTURES:
        if f.kind != "disputed":
            continue
        fired = sorted(_fired(f))
        lines.append(f"{f.id}: {', '.join(fired) or 'ничего не сработало'}")
    assert lines, "disputed-фикстуры пропали из датасета"
    assert CLEAN, "clean-фикстуры пропали из датасета"
    print("DISPUTED-ОТЧЁТ: " + " | ".join(lines))
