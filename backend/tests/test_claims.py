"""Утверждения проекта: подтверждение по документам и полнота.

Этап 4 ТЗ. Проверяется не «текст похож на правду», а конкретная связь
«утверждение → документ → цитата». Утверждение без источника получает
явный статус, а не молчаливое подтверждение.
"""

from __future__ import annotations

from decimal import Decimal

from app.verification.claims import build_claims, check_completeness, split_claims
from app.verification.contracts import ClaimStatus, ClaimType

DOCUMENTS = {
    "иск.docx": (
        "Истец просит взыскать долг 80 000 руб. и проценты 9 122,19 руб. "
        "Договор займа заключён 10.02.2024."
    ),
    "возражение.docx": "Ответчик передал 8 000 руб. платёжным поручением.",
}


class TestSplit:
    def test_short_fragments_are_not_claims(self) -> None:
        assert split_claims("Итого. 1. РЕШИЛ.") == []

    def test_factual_sentence_is_claim(self) -> None:
        claims = split_claims("Договор займа заключён 10 февраля 2024 года.")
        assert len(claims) == 1


class TestBuildClaims:
    def test_claim_with_number_from_document_is_supported(self) -> None:
        claims = build_claims(
            "Судом установлено, что истец просит взыскать долг 80 000 руб.",
            DOCUMENTS,
        )
        assert claims
        claim = claims[0]
        assert claim.claim_type is ClaimType.AMOUNT
        assert claim.support_status is ClaimStatus.SUPPORTED
        assert claim.evidence[0].document == "иск.docx"
        assert "80 000" in claim.evidence[0].quote

    def test_invented_amount_is_not_supported(self) -> None:
        claims = build_claims(
            "Взыскать расходы на представителя 6 470 руб. с ответчика.",
            DOCUMENTS,
        )
        claim = claims[0]
        assert claim.support_status is ClaimStatus.INSUFFICIENT_EVIDENCE
        assert claim.review_required is True
        assert "6 470" in claim.review_reason

    def test_invented_date_is_not_supported(self) -> None:
        claims = build_claims(
            "Договор займа заключён между сторонами 15.07.2025 в городе.",
            DOCUMENTS,
        )
        assert claims[0].support_status is ClaimStatus.INSUFFICIENT_EVIDENCE

    def test_claim_without_numbers_is_not_auto_confirmed(self) -> None:
        claims = build_claims(
            "Суд оценил представленные сторонами доказательства в совокупности.",
            DOCUMENTS,
        )
        assert claims[0].support_status is ClaimStatus.NOT_APPLICABLE


class TestCompleteness:
    def test_missing_requirement_is_reported(self) -> None:
        draft = "Взыскать долг 80 000 руб."
        missing = check_completeness(draft, [Decimal("80000"), Decimal("9122.19")])
        assert len(missing) == 1
        assert "9 122" in missing[0].text
        assert missing[0].review_required is True

    def test_all_requirements_present(self) -> None:
        draft = "Взыскать 80 000 руб. и 9 122,19 руб."
        assert check_completeness(draft, [Decimal("80000"), Decimal("9122.19")]) == []
