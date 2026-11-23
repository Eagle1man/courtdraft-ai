"""Единая точка расчёта госпошлины — с обязательной датой подачи.

В репозитории две реализации: verification/laws.state_fee знает редакции по
дате, verification/state_fee.claim_fee — нет. Конструктор иска пользовался
второй и считал архивные дела по действующей сетке. Здесь один вход,
делегированный знающей редакции реализации.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.verification.laws import state_fee as _state_fee_by_date
from app.verification.state_fee import arbitration_fee as _arbitration_fee

EDITION_NOTE = "ст. 333.19 НК РФ, редакция по дате подачи иска"


def claim_fee_kopecks(price_kopecks: int, on: date | None) -> int:
    """Пошлина по имущественному иску в копейках, по редакции на дату подачи."""
    rubles = Decimal(price_kopecks) / Decimal(100)
    fee = Decimal(str(_state_fee_by_date(float(rubles), on)))
    return int((fee * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def arbitration_fee_kopecks(price_kopecks: int) -> int:
    rubles = Decimal(price_kopecks) / Decimal(100)
    result = _arbitration_fee(rubles)
    return int(
        (Decimal(result.amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
