from __future__ import annotations

from datetime import date

import pytest

from app.verification.interest import (
    RatesError,
    accrue,
    closure,
    days_in_year,
    rate_on,
)

PRINCIPAL = 9_000_000          # 90 000,00 руб.
START = date(2024, 8, 11)
END = date(2025, 3, 5)
RAW_TOTAL = 1_022_627          # по фактическим ставкам
TARGET = 1_026_329             # принято судом


def test_rate_table_covers_2016() -> None:
    assert rate_on(date(2016, 3, 1)) is not None
    assert rate_on(date(2024, 12, 1)) is not None


def test_days_in_year_leap() -> None:
    assert days_in_year(2024) == 366
    assert days_in_year(2025) == 365


def test_accrue_raw_total() -> None:
    result = accrue(PRINCIPAL, START, END)
    assert result.total_kopecks == RAW_TOTAL
    assert sum(s.days for s in result.slices) == (END - START).days + 1
    assert not result.gaps


def test_etalon_closure_exact() -> None:
    result = accrue(PRINCIPAL, START, END)
    closed = closure(TARGET, result, mode="ETALON")
    assert closed.applied is True
    assert closed.total_kopecks == TARGET
    assert sum(s.amount_kopecks for s in closed.slices) == TARGET
    assert closed.delta_kopecks == TARGET - RAW_TOTAL


def test_clean_mode_recomputes_totals() -> None:
    result = accrue(PRINCIPAL, START, END)
    clean = closure(TARGET, result, mode="CLEAN")
    assert clean.applied is False
    assert clean.total_kopecks == RAW_TOTAL
    assert clean.delta_kopecks == TARGET - RAW_TOTAL


def test_missing_rates_file_raises(monkeypatch) -> None:
    import app.verification.interest as interest

    monkeypatch.setattr(interest, "_PERIODS", None)
    monkeypatch.setattr(interest, "RATES_PATH", interest.RATES_PATH.parent / "нет.json")
    with pytest.raises(RatesError):
        interest.rate_periods()
