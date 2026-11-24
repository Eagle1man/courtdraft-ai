"""Сопоставление фактов между документами.

Все значимые варианты сохраняются. Конфликт не разрешается выбором «последнего»:
поле остаётся пустым, решение принимает человек.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from .models import (
    Conflict,
    FactCandidate,
    FactStatus,
    ValidationFinding,
)

BLOCKING_ROLES = {
    "debt_amount",
    "claim_amount",
    "claim_total",
    "plaintiff_name",
    "defendant_name",
    "court_name",
    "contract_date",
    "obligation_amount",
    "interest_amount",
    "fee_paid",
}
_MARKER = re.compile(r"^\[[^\]]+ — по данным дела\]$", re.I)


def _norm_name(value: str) -> str:
    normalized = re.sub(r"[«»\"']", "", value or "").lower()
    normalized = re.sub(
        r"\b(ооо|оао|зао|пао|ао|ип|фгуп|гуп|муп)\b", "", normalized
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    tokens = normalized.split()
    # Полное ФИО и «Фамилия И.О.» — одна сущность внутри одной роли.
    if len(tokens) >= 2 and all(re.fullmatch(r"[а-яё.-]+", token) for token in tokens):
        return tokens[0]
    return normalized


def _is_marker(value: Any) -> bool:
    return isinstance(value, str) and bool(_MARKER.match(value.strip()))


def _distinct_key(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, list):
        return "|".join(sorted(str(item) for item in value))
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip().casefold()
    return str(value)


def _best(group: list[FactCandidate]) -> FactCandidate:
    priority = {
        FactStatus.SUPPORTED: 3,
        FactStatus.DERIVED: 3,
        FactStatus.NEEDS_REVIEW: 2,
        FactStatus.AMBIGUOUS: 1,
    }
    return max(
        group,
        key=lambda fact: (
            priority.get(fact.status, 0),
            fact.confidence,
            len(fact.quote),
        ),
    )


def _variants(group: list[FactCandidate]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for fact in group:
        output.append(
            {
                "value": str(fact.value_norm if fact.value_norm is not None else fact.value_raw),
                "raw": fact.value_raw,
                "documents": [fact.locator.filename],
                "document_id": fact.locator.document_id,
                "page": fact.locator.page,
                "quotes": [fact.quote[:300]],
                "fact_id": fact.id,
                "status": fact.status.value,
            }
        )
    return output


def reconcile(
    facts: list[FactCandidate], roles: dict[str, dict[str, Any]]
) -> tuple[dict[str, FactCandidate | None], list[Conflict], list[ValidationFinding]]:
    by_role: dict[str, list[FactCandidate]] = {}
    for fact in facts:
        if fact.status != FactStatus.UNSUPPORTED:
            by_role.setdefault(fact.role, []).append(fact)

    chosen: dict[str, FactCandidate | None] = {}
    conflicts: list[Conflict] = []
    findings: list[ValidationFinding] = []

    for role, spec in roles.items():
        group = by_role.get(role, [])
        # Детерминированный разбор приоритетен для того же поля: LLM может
        # найти цитату, но неверно интерпретировать роль (например, назвать
        # остаток долга суммой договора). LLM-факты остаются в analysis,
        # но не создают конфликт с надёжным regex-результатом.
        deterministic_group = [
            fact for fact in group
            if fact.model == "regex" and fact.status != FactStatus.UNSUPPORTED
        ]
        if deterministic_group:
            group = deterministic_group
        if not group:
            chosen[role] = None
            if spec.get("required"):
                findings.append(
                    ValidationFinding(
                        level="warning",
                        code="NOT_FOUND",
                        field=role,
                        message=(
                            f"нет сведений для поля «{role}» — "
                            "проверьте комплект документов"
                        ),
                    )
                )
            continue

        kind = spec.get("kind", "text")
        if kind == "list":
            values: list[str] = []
            source_ids: list[str] = []
            for fact in group:
                items = fact.value_norm if isinstance(fact.value_norm, list) else [fact.value_norm]
                for item in items or []:
                    value = str(item).strip()
                    if value and value not in values:
                        values.append(value)
                source_ids.append(fact.id)
            if values:
                best = _best(group)
                best.value_norm = values
                best.source_fact_ids = source_ids
                chosen[role] = best
            else:
                chosen[role] = None
            continue

        # Маркер отсутствия не конфликтует с реально найденным значением.
        real = [fact for fact in group if not _is_marker(fact.value_norm)]
        markers = [fact for fact in group if _is_marker(fact.value_norm)]
        if real:
            group = real
        elif markers:
            chosen[role] = None
            continue

        distinct: dict[str, list[FactCandidate]] = {}
        for fact in group:
            key = _distinct_key(fact.value_norm)
            if kind == "name":
                key = _norm_name(str(fact.value_norm or fact.value_raw))
            distinct.setdefault(key, []).append(fact)

        if len(distinct) > 1:
            if kind in {"money", "money_kopecks"}:
                conflict_kind = "amount"
            elif kind in {"date", "date_ru"}:
                conflict_kind = "date"
            elif kind == "name":
                conflict_kind = "name"
            else:
                conflict_kind = "other"
            conflicts.append(
                Conflict(
                    role=role,
                    kind=conflict_kind,
                    variants=_variants(group),
                    blocking=bool(spec.get("required")) or role in BLOCKING_ROLES,
                    note="значения расходятся — выбор за пользователем",
                )
            )
            chosen[role] = None
            for fact in group:
                fact.status = FactStatus.CONFLICT
            continue

        chosen[role] = _best(group)

    order = ["contract_date", "contract_due_date", "pretrial_date", "suit_date"]
    previous: tuple[str, str] | None = None
    for role in order:
        date_fact = chosen.get(role)
        if date_fact and date_fact.value_norm:
            current = str(date_fact.value_norm)
            if previous and current < previous[1]:
                findings.append(
                    ValidationFinding(
                        level="warning",
                        code="CHRONOLOGY",
                        field=role,
                        message=(
                            f"{role} раньше {previous[0]} — проверьте виды дат "
                            "(составление/подписание/отправка/получение)"
                        ),
                    )
                )
            previous = (role, current)
    return chosen, conflicts, findings


def arithmetic_checks(facts: list[FactCandidate]) -> list[ValidationFinding]:
    """Сверка: обязательство − оплаты ≈ заявленный долг. Только Decimal."""

    def one(role: str) -> Decimal | None:
        values = [
            fact.value_norm
            for fact in facts
            if fact.role == role and isinstance(fact.value_norm, Decimal)
        ]
        return values[0] if len(values) == 1 else None

    obligation = one("obligation_amount")
    paid = one("paid_amount")
    debt = one("debt_amount")
    if obligation is None or paid is None or debt is None:
        return []
    derived = obligation - paid
    if derived != debt:
        return [
            ValidationFinding(
                level="warning",
                code="ARITHMETIC",
                field="debt_amount",
                message=(
                    f"обязательство {obligation} − оплаты {paid} = {derived}, "
                    f"но заявлен долг {debt} — сверить расчёт"
                ),
            )
        ]
    return []
