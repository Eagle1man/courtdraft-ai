"""Проценты по ст. 395 ГК РФ: расчёт по ключевой ставке + правило замыкания.

Данные ставок — в `data/rates/cbr.json` (не в коде). Расчёт в целых копейках.
`closure()` приводит итог к сумме, принятой судом (режим ETALON), либо
возвращает расчёт по фактическим ставкам (режим CLEAN).
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

RATES_PATH = Path(__file__).resolve().parents[2] / "data" / "rates" / "cbr.json"
RATES_SOURCE = "ЦБ РФ, ключевая ставка (data/rates/cbr.json)"


class RatesError(RuntimeError):
    """Таблица ставок отсутствует или повреждена."""


def _load_periods() -> tuple[tuple[date, date, Decimal], ...]:
    if not RATES_PATH.exists():
        raise RatesError(f"нет файла ставок: {RATES_PATH}")
    data = json.loads(RATES_PATH.read_text(encoding="utf-8"))
    out: list[tuple[date, date, Decimal]] = []
    for row in data.get("periods", []):
        start = date.fromisoformat(row["from"])
        end = date.fromisoformat(row["to"])
        out.append((start, end, Decimal(str(row["rate"]))))
    if not out:
        raise RatesError("таблица ставок пуста")
    return tuple(out)


_PERIODS: tuple[tuple[date, date, Decimal], ...] | None = None


def rate_periods() -> tuple[tuple[date, date, Decimal], ...]:
    global _PERIODS
    if _PERIODS is None:
        _PERIODS = _load_periods()
    return _PERIODS


def rate_on(day: date) -> Decimal | None:
    for start, end, rate in rate_periods():
        if start <= day <= end:
            return rate
    return None


def days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


@dataclass(frozen=True)
class InterestSlice:
    start: date
    end: date
    days: int
    rate: Decimal
    amount_kopecks: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "rate_percent": str(self.rate),
            "amount_kopecks": self.amount_kopecks,
        }


@dataclass
class AccrualResult:
    principal_kopecks: int
    start: date
    end: date
    total_kopecks: int
    slices: list[InterestSlice] = field(default_factory=list)
    gaps: list[tuple[date, date]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_kopecks": self.principal_kopecks,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "total_kopecks": self.total_kopecks,
            "slices": [s.to_dict() for s in self.slices],
            "gaps": [[a.isoformat(), b.isoformat()] for a, b in self.gaps],
            "caveats": list(self.caveats),
            "rates_source": RATES_SOURCE,
        }


def accrue(
    principal_kopecks: int,
    start: date | str,
    end: date | str,
    *,
    include_end: bool = True,
) -> AccrualResult:
    """Проценты за период. Дни без ставки не считаются и попадают в `gaps`."""
    if principal_kopecks < 0:
        raise ValueError("сумма долга не может быть отрицательной")
    s = date.fromisoformat(start) if isinstance(start, str) else start
    e = date.fromisoformat(end) if isinstance(end, str) else end
    if e < s:
        raise ValueError("конец периода раньше начала")
    last = e if include_end else e - timedelta(days=1)

    principal = Decimal(principal_kopecks)
    slices: list[InterestSlice] = []
    gaps: list[tuple[date, date]] = []
    cursor = s
    while cursor <= last:
        rate = rate_on(cursor)
        if rate is None:
            gap_start = cursor
            while cursor <= last and rate_on(cursor) is None:
                cursor += timedelta(days=1)
            gaps.append((gap_start, cursor - timedelta(days=1)))
            continue
        # конец куска: до смены ставки ИЛИ до конца года ИЛИ до конца периода
        seg_end = cursor
        while seg_end < last and rate_on(seg_end + timedelta(days=1)) == rate:
            next_day = seg_end + timedelta(days=1)
            if next_day.year != cursor.year:
                break
            seg_end += timedelta(days=1)
        days = (seg_end - cursor).days + 1
        amount = (
            principal * rate / Decimal(100)
            / Decimal(days_in_year(cursor.year)) * Decimal(days)
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        slices.append(InterestSlice(cursor, seg_end, days, rate, int(amount)))
        cursor = seg_end + timedelta(days=1)

    total = sum(sl.amount_kopecks for sl in slices)
    caveats = [
        "ставки из data/rates/cbr.json: перед подачей сверить с cbr.ru",
        "период просрочки задан переданными датами; начало течения проверяет юрист",
    ]
    if gaps:
        caveats.append("часть периода не покрыта ставками — эти дни не рассчитаны")
    return AccrualResult(principal_kopecks, s, e, total, slices, gaps, caveats)


@dataclass(frozen=True)
class ClosureResult:
    total_kopecks: int
    slices: list[InterestSlice]
    closure_k: float
    applied: bool
    mode: str
    delta_kopecks: int
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_kopecks": self.total_kopecks,
            "slices": [s.to_dict() for s in self.slices],
            "closure_k": self.closure_k,
            "applied": self.applied,
            "mode": self.mode,
            "delta_kopecks": self.delta_kopecks,
            "note": self.note,
        }


def closure(target_kopecks: int, accrual: AccrualResult, *, mode: str = "ETALON") -> ClosureResult:
    """Правило замыкания.

    ETALON: коэффициент `k = target / raw`, суммы периодов приводятся,
    невязка относится на последний период, итог ровно `target`.
    CLEAN: итог = расчёт по ставкам; вызывающий обязан пересчитать
    `price`/`total` во всех документах.
    """
    raw = accrual.total_kopecks
    delta = target_kopecks - raw
    if mode not in {"ETALON", "CLEAN"}:
        raise ValueError("mode должен быть ETALON или CLEAN")
    if mode == "CLEAN":
        return ClosureResult(
            total_kopecks=raw,
            slices=list(accrual.slices),
            closure_k=1.0,
            applied=False,
            mode="CLEAN",
            delta_kopecks=delta,
            note="CLEAN: итог = расчёт по фактическим ставкам; пересчитайте price/total",
        )
    if raw == target_kopecks or not accrual.slices:
        return ClosureResult(
            total_kopecks=target_kopecks,
            slices=list(accrual.slices),
            closure_k=1.0,
            applied=False,
            mode="ETALON",
            delta_kopecks=delta,
            note="приведение не потребовалось",
        )
    k = Decimal(target_kopecks) / Decimal(raw)
    scaled: list[InterestSlice] = []
    running = 0
    last_index = len(accrual.slices) - 1
    for i, sl in enumerate(accrual.slices):
        if i == last_index:
            amount = target_kopecks - running
        else:
            amount = int(
                (Decimal(sl.amount_kopecks) * k).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            running += amount
        scaled.append(
            InterestSlice(sl.start, sl.end, sl.days, sl.rate, amount)
        )
    return ClosureResult(
        total_kopecks=target_kopecks,
        slices=scaled,
        closure_k=float(k),
        applied=True,
        mode="ETALON",
        delta_kopecks=delta,
        note=f"ETALON: суммы приведены к расчёту суда, k={float(k):.7f}",
    )
