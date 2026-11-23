"""Журнал расхождений: что в материалах и что по закону.

В режиме ETALON значения берутся из паспорта (они воспроизводят исходное
решение), а расхождения с законом показываются в журнале и в текст документа
не попадают. В режиме CLEAN расчёт по закону применяется, и об этом тоже
остаётся запись.
"""
from __future__ import annotations

from typing import Any

from app.drafting.claim_model import ClaimModel
from app.fees import claim_fee_kopecks
from app.ingestion.passport import CasePassport, parse_ru_date
from app.verification.jurisdiction import expected_tier


def build_deviations(
    model: ClaimModel, passport: CasePassport | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    mode = model.mode

    expected = expected_tier(model.price / 100)
    court_low = model.court_for_header.lower()
    actual = "мировой судья" if "миров" in court_low or "участок" in court_low else "районный суд"
    if expected != actual:
        rows.append(
            {
                "code": "X09",
                "item": "подсудность",
                "in_materials": actual,
                "by_law": f"{expected} (ст. 23, 24 ГПК РФ)",
                "resolution": (
                    "ETALON: суд из паспорта; расхождение только в журнале"
                    if mode == "ETALON"
                    else "CLEAN: суд приведён в соответствие с подсудностью"
                ),
                "severity": "P1",
            }
        )

    filed = parse_ru_date(model.suit_date)
    by_law = claim_fee_kopecks(model.price, filed)
    if by_law != model.fee_paid:
        rows.append(
            {
                "code": "FEE",
                "item": "госпошлина",
                "in_materials_kopecks": model.fee_paid,
                "by_law_kopecks": by_law,
                "delta_kopecks": by_law - model.fee_paid,
                "resolution": (
                    "ETALON: в тексте уплаченная сумма; расчёт по НК РФ — в журнале"
                    if mode == "ETALON"
                    else "CLEAN: пошлина пересчитана, итог изменён согласованно"
                ),
                "severity": "P1",
            }
        )

    if passport is not None and passport.interest.closure_applied:
        raw = passport.interest.raw_sum_kopecks
        delta = passport.interest.delta_kopecks
        rows.append(
            {
                "code": "INT_CLOSURE",
                "item": "проценты по ст. 395 ГК РФ",
                "in_materials_kopecks": model.interest,
                "by_rates_kopecks": raw,
                "delta_kopecks": delta,
                "closure_k": passport.interest.closure_k,
                "resolution": "ETALON: приведение к расчёту, принятому судом",
                "severity": "P1",
            }
        )

    if filed is not None:
        end = parse_ru_date(model.interest_to)
        if end is not None and end > filed:
            rows.append(
                {
                    "code": "INT_PERIOD",
                    "item": "конец периода процентов",
                    "in_materials": model.interest_to,
                    "by_law": (
                        "период обычно заканчивается датой подачи "
                        "либо заявляется по дату исполнения"
                    ),
                    "resolution": "ETALON: период из решения сохранён",
                    "severity": "P1",
                }
            )

    for item in passport.deviations if passport is not None else []:
        if not any(r["code"] == item.code for r in rows):
            rows.append(item.model_dump(mode="json"))
    return rows
