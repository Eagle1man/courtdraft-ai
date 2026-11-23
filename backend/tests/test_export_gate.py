"""Гейт экспорта: DOCX нельзя выгрузить при P0 или устаревшем отчёте."""
from __future__ import annotations

import pytest


def test_blocked_on_p0() -> None:
    from app.services.export_gate import ExportBlocked, assert_export_allowed

    with pytest.raises(ExportBlocked):
        assert_export_allowed(p0_count=1, is_stale=False, mode="final")


def test_blocked_on_stale() -> None:
    from app.services.export_gate import ExportBlocked, assert_export_allowed

    with pytest.raises(ExportBlocked):
        assert_export_allowed(p0_count=0, is_stale=True, mode="final")


def test_draft_mode_allowed_with_p0() -> None:
    from app.services.export_gate import assert_export_allowed

    assert_export_allowed(p0_count=3, is_stale=True, mode="draft")


def test_court_attribute_fixed() -> None:
    """В export-роуте не должно быть getattr(case, 'court', '')."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "routes" / "drafts.py"
    ).read_text(encoding="utf-8")
    assert 'getattr(case, "court", "")' not in source
