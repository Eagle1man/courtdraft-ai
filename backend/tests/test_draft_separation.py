"""Иск и решение — разные проекты с независимыми историями версий."""
from __future__ import annotations

import inspect


def test_repository_signature_has_kind() -> None:
    from app.db.repositories import DraftRepository

    sig = inspect.signature(DraftRepository.get_for_case)
    assert "kind" in sig.parameters


def test_unique_index_declared() -> None:
    from app.models.entities import Draft

    names = {c.name for c in Draft.__table__.constraints} | {
        i.name for i in Draft.__table__.indexes
    }
    assert any("kind" in (n or "") for n in names), names
