"""Матрица требований и согласованность частей проекта (Этап 5 ТЗ).

Матрица отвечает на вопрос «что просили и что с этим стало»: каждое
денежное требование из материалов сопоставляется с возражениями,
документами и текстом проекта. Непокрытое требование — это пробел, а
не «всё хорошо».

Согласованность проверяет то, что видно только при сравнении частей:
одно и то же требование названо в проекте двумя разными суммами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.agents.evidence_pack import EvidencePack
from app.verification.deterministic import Finding, amounts_by_purpose


@dataclass(frozen=True)
class MatrixRow:
    """Строка матрицы: требование и его окружение."""

    requirement: str
    amount: Decimal | None = None
    objections: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    covered: bool = False


def _money(value: str) -> Decimal | None:
    cleaned = value.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except Exception:  # noqa: BLE001 - значение приходит из текста
        return None


def build_matrix(pack: EvidencePack, draft_text: str) -> list[MatrixRow]:
    """Сопоставить требования материалов с проектом.

    Требования берутся из размеченных фраз пакета (категория
    `claims`), возражения — из `objections`. Наличие суммы в проекте
    проверяется по тексту.
    """
    text = draft_text or ""
    objections = [item.text for item in pack.by_category("objections")]
    evidence_docs = [item.source_document for item in pack.items if item.source_document]

    rows: list[MatrixRow] = []
    for item in pack.by_category("claims"):
        if not item.amounts:
            continue
        amount = _money(item.amounts[0])
        if amount is None:
            continue
        spaced = f"{int(amount):,}".replace(",", " ")
        rows.append(
            MatrixRow(
                requirement=item.text,
                amount=amount,
                objections=objections,
                evidence=sorted(set(evidence_docs)),
                covered=spaced in text or str(int(amount)) in text,
            )
        )
    return rows


def check_consistency(draft_text: str) -> list[Finding]:
    """Одно требование — одна сумма.

    Если в проекте «долг 80 000» и «долг 60 000», это либо спорный
    факт, который судья обязан увидеть, либо ошибка модели. Молча
    выбирать нельзя.
    """
    by_purpose: dict[str, set[Decimal]] = {}
    for purpose, value, _ in amounts_by_purpose(draft_text or ""):
        by_purpose.setdefault(purpose, set()).add(value)

    findings: list[Finding] = []
    for purpose, values in by_purpose.items():
        if len(values) < 2:
            continue
        shown = ", ".join(str(value) for value in sorted(values))
        findings.append(
            Finding(
                check_id="CONSISTENCY_AMOUNT_CONFLICT",
                severity="HIGH",
                reason=(
                    f"требование «{purpose}» названо в проекте "
                    f"разными суммами: {shown}"
                ),
                fragment=purpose,
            )
        )
    return findings
