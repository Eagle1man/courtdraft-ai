"""Разбиение проекта на проверяемые утверждения (Этап 4 ТЗ).

Зачем: проверка, которая смотрит на раздел целиком, пропускает
отдельное неверное утверждение внутри верного абзаца. Здесь текст
делится на фразы, и каждая проверяется отдельно:

* есть ли её числа и даты в материалах;
* каким документом она подтверждается;
* есть ли требование, которое в проект не попало (полнота).

Модуль ничего не сохраняет и не меняет статус документа: он готовит
`Claim` для отчёта (Этап 9) и для правил допуска (Этап 5).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Iterable

from app.verification.contracts import (
    Claim,
    ClaimStatus,
    ClaimType,
    EvidenceReference,
)
from app.verification.deterministic import _dates, amounts_by_purpose

#: Минимальная длина фразы, которая считается утверждением. Короткие
#: обрывки («1.», «Итого») ничего не утверждают.
_MIN_CLAIM_CHARS = 40

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

#: Слова, по которым видно, что фраза что-то утверждает о факте.
_FACT_MARKERS = (
    "заключ", "переда", "получ", "возврат", "уплат", "оплат", "взыск",
    "являет", "состав", "призна", "отказ", "наруш", "начисл", "составл",
)


def split_claims(text: str) -> list[str]:
    """Разбить текст на фразы-утверждения.

    Фраза попадает в разбор, если в ней есть число, признак факта или
    хотя бы шесть слов. Оценочные фразы без чисел тоже нужны: их надо
    показать как «правилами не проверено», а не терять — иначе полнота
    декомпозиции неизмерима (ТЗ §9.2).
    """
    claims: list[str] = []
    for raw in _SENTENCE_RE.split((text or "").replace("\n", " ")):
        fragment = raw.strip()
        if len(fragment) < _MIN_CLAIM_CHARS:
            continue
        low = fragment.lower()
        has_number = bool(re.search(r"\d", fragment))
        words = re.findall(r"[А-Яа-яЁёA-Za-z]{2,}", fragment)
        if has_number or any(marker in low for marker in _FACT_MARKERS) or len(words) >= 6:
            claims.append(fragment)
    return claims


def _documents_with(needle: str, documents: dict[str, str]) -> list[EvidenceReference]:
    """Документы, где встречается число или дата."""
    refs: list[EvidenceReference] = []
    for name, text in documents.items():
        if needle and needle in (text or ""):
            refs.append(EvidenceReference(document=name, quote=needle, exact=True))
    return refs


def _quote_around(text: str, needle: str, width: int = 120) -> str:
    index = (text or "").find(needle)
    if index < 0:
        return ""
    return text[max(0, index - width): index + len(needle) + width].strip()


def build_claims(
    text: str,
    documents: dict[str, str],
    *,
    section: str = "",
) -> list[Claim]:
    """Проверить каждое утверждение по материалам дела.

    Правило: утверждение считается подтверждённым, только если его
    число или дата встречается в конкретном документе, и этот документ
    сохраняется как свидетельство. Иначе — `INSUFFICIENT_EVIDENCE` и
    требование проверить вручную.
    """
    documents = documents or {}
    claims: list[Claim] = []

    for index, fragment in enumerate(split_claims(text), start=1):
        amounts = amounts_by_purpose(fragment)
        dates = _dates(fragment)

        evidence: list[EvidenceReference] = []
        missing: list[str] = []

        for _, value, raw in amounts:
            needle = str(value).rstrip("0").rstrip(".") if value == value.to_integral() else str(value)
            # Число в документе может быть записано с разрядами —
            # ищем и «80000», и «80 000».
            spaced = f"{int(value):,}".replace(",", " ")
            refs = _documents_with(spaced, documents) or _documents_with(
                str(int(value)), documents
            ) or _documents_with(needle, documents)
            if refs:
                evidence.extend(
                    EvidenceReference(
                        document=ref.document,
                        quote=_quote_around(documents[ref.document], ref.quote),
                    )
                    for ref in refs
                )
            else:
                missing.append(raw)

        for day, month, year in dates:
            as_dot = f"{day:02d}.{month:02d}.{year}"
            refs = _documents_with(as_dot, documents)
            if refs:
                evidence.extend(refs)
            else:
                missing.append(as_dot)

        if missing:
            status = ClaimStatus.INSUFFICIENT_EVIDENCE
        elif evidence:
            status = ClaimStatus.SUPPORTED
        else:
            # Утверждение без чисел и дат правилами не проверяется:
            # это не «подтверждено», а «не проверялось этим валидатором».
            status = ClaimStatus.NOT_APPLICABLE

        claim_type = ClaimType.FACT
        if amounts:
            claim_type = ClaimType.AMOUNT
        elif dates:
            claim_type = ClaimType.DATE

        claims.append(
            Claim(
                claim_id=f"{section or 'draft'}-{index}",
                text=fragment,
                claim_type=claim_type,
                section=section,
                materiality="HIGH" if missing else "MEDIUM",
                evidence=evidence,
                support_status=status,
                review_required=bool(missing),
                review_reason=(
                    "нет в материалах: " + ", ".join(sorted(set(missing)))
                    if missing
                    else ""
                ),
            )
        )
    return claims


def summarize_claims(claims: Iterable[Claim]) -> dict[str, int]:
    """Короткая сводка по утверждениям для отчёта.

    Нужна интерфейсу, чтобы показать «сколько проверено, сколько требует
    внимания», не разбирая список на клиенте.
    """
    items = list(claims)
    return {
        "total": len(items),
        "supported": sum(
            1 for c in items if c.support_status is ClaimStatus.SUPPORTED
        ),
        "insufficient": sum(
            1 for c in items
            if c.support_status is ClaimStatus.INSUFFICIENT_EVIDENCE
        ),
        "not_applicable": sum(
            1 for c in items if c.support_status is ClaimStatus.NOT_APPLICABLE
        ),
        "review_required": sum(1 for c in items if c.review_required),
    }


def check_completeness(
    draft_text: str,
    requirement_amounts: Iterable[Decimal],
    *,
    section: str = "completeness",
) -> list[Claim]:
    """Требования, которые не попали в проект (Тест K ТЗ).

    Проверяется только наличие суммы требования в тексте. Если сумма
    есть, это ещё не значит, что требование рассмотрено по существу —
    такая неопределённость остаётся и показывается судье.
    """
    text = draft_text or ""
    claims: list[Claim] = []
    for index, value in enumerate(requirement_amounts, start=1):
        spaced = f"{int(value):,}".replace(",", " ")
        compact = str(int(value))
        present = spaced in text or compact in text
        if present:
            continue
        claims.append(
            Claim(
                claim_id=f"{section}-{index}",
                text=f"требование на сумму {spaced} руб.",
                claim_type=ClaimType.AMOUNT,
                section=section,
                materiality="CRITICAL",
                support_status=ClaimStatus.INSUFFICIENT_EVIDENCE,
                review_required=True,
                review_reason=(
                    "требование из материалов не отражено в проекте — "
                    "проверить полноту"
                ),
            )
        )
    return claims
