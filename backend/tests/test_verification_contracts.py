"""Контракты проверки: статусы, устаревание, представление отчёта.

Этап 2 ТЗ. Проверяется именно интерпретация: один и тот же набор
результатов должен давать один и тот же статус, а старый отчёт не
должен выглядеть как проверка нового текста.
"""

from __future__ import annotations

from app.verification.contracts import (
    CheckResult,
    Claim,
    ClaimStatus,
    ClaimType,
    DocumentStatus,
    EvidenceReference,
    ScopeReport,
    decide_status,
    from_legacy_status,
    is_stale,
    scope_report,
    text_checksum,
)


class TestDecideStatus:
    def test_failed_check_overrides_everything(self) -> None:
        # Технический сбой важнее любых «хороших» показателей.
        assert (
            decide_status(checks_failed=True, critical=0, verified_checks=5)
            is DocumentStatus.VERIFICATION_FAILED
        )

    def test_stale_report_is_not_verified(self) -> None:
        assert (
            decide_status(stale=True, verified_checks=5)
            is DocumentStatus.REVIEW_REQUIRED
        )

    def test_critical_blocks(self) -> None:
        assert (
            decide_status(critical=1, verified_checks=5)
            is DocumentStatus.BLOCKED
        )

    def test_no_checks_is_not_success(self) -> None:
        # Отсутствие проверок — не «всё хорошо».
        assert decide_status(verified_checks=0) is DocumentStatus.REVIEW_REQUIRED

    def test_review_required(self) -> None:
        assert (
            decide_status(review=2, verified_checks=3)
            is DocumentStatus.REVIEW_REQUIRED
        )

    def test_verified_within_scope(self) -> None:
        assert (
            decide_status(verified_checks=3)
            is DocumentStatus.VERIFIED_WITHIN_SCOPE
        )

    def test_legacy_mapping(self) -> None:
        assert from_legacy_status("PASS") is DocumentStatus.VERIFIED_WITHIN_SCOPE
        assert from_legacy_status("BLOCKED") is DocumentStatus.BLOCKED
        assert from_legacy_status("REVIEW_REQUIRED") is DocumentStatus.REVIEW_REQUIRED
        # Неизвестное значение не должно выглядеть как успех.
        assert from_legacy_status("что-то") is DocumentStatus.REVIEW_REQUIRED


class TestStaleness:
    def test_same_version(self) -> None:
        assert is_stale(3, 3) is False

    def test_newer_version(self) -> None:
        assert is_stale(2, 3) is True

    def test_checksum_changes_with_text(self) -> None:
        assert text_checksum("текст") != text_checksum("текст!")
        assert text_checksum("") == text_checksum("")


class TestClaimModel:
    def test_contradiction_does_not_erase_support(self) -> None:
        claim = Claim(
            claim_id="c1",
            text="Ответчик передал 20 000 руб. наличными.",
            claim_type=ClaimType.AMOUNT,
            support_status=ClaimStatus.SUPPORTED,
            contradiction_status=ClaimStatus.CONTRADICTED,
        )
        # Оба статуса сохраняются: подтверждение не отменяет противоречие.
        assert claim.support_status is ClaimStatus.SUPPORTED
        assert claim.contradiction_status is ClaimStatus.CONTRADICTED
        assert claim.blocked is True

    def test_evidence_exactness_is_explicit(self) -> None:
        ref = EvidenceReference(document="иск.docx", quote="80 000 руб.")
        assert ref.exact is True
        unclear = EvidenceReference(document="иск.docx", locator="страница неизвестна", exact=False)
        assert unclear.exact is False

    def test_serialization_roundtrip(self) -> None:
        claim = Claim(
            claim_id="c2",
            text="Договор заключён 10.02.2024.",
            claim_type=ClaimType.DATE,
            evidence=[EvidenceReference(document="иск.docx", quote="10.02.2024")],
        )
        restored = Claim.model_validate(claim.model_dump())
        assert restored == claim


class TestScopeReport:
    def _claim(self, status: ClaimStatus, review: bool = False) -> Claim:
        return Claim(
            claim_id="x",
            text="утверждение",
            claim_type=ClaimType.FACT,
            support_status=status,
            review_required=review,
        )

    def test_report_status_follows_claims(self) -> None:
        claims = [
            Claim(
                claim_id="c",
                text="иск удовлетворён",
                claim_type=ClaimType.FACT,
                support_status=ClaimStatus.SUPPORTED,
                # Проверка должна быть выполнена: утверждение без
                # результатов — это не «проверено».
                results=[
                    CheckResult(
                        check_id="source_support",
                        status=ClaimStatus.SUPPORTED,
                        validator="evidence",
                    )
                ],
            )
        ]
        report = scope_report(draft_version=1, text="текст", claims=claims)
        assert isinstance(report, ScopeReport)
        assert report.status is DocumentStatus.VERIFIED_WITHIN_SCOPE
        assert report.text_sha256 == text_checksum("текст")

    def test_claim_without_checks_is_not_verified(self) -> None:
        claims = [self._claim(ClaimStatus.SUPPORTED)]
        report = scope_report(draft_version=1, text="текст", claims=claims)
        assert report.status is DocumentStatus.REVIEW_REQUIRED

    def test_contradicted_claim_blocks_report(self) -> None:
        claims = [
            Claim(
                claim_id="x",
                text="долг 60 000",
                claim_type=ClaimType.AMOUNT,
                support_status=ClaimStatus.SUPPORTED,
                contradiction_status=ClaimStatus.CONTRADICTED,
            )
        ]
        report = scope_report(draft_version=1, text="текст", claims=claims)
        assert report.status is DocumentStatus.BLOCKED
        assert report.critical_claims

    def test_review_claim_requires_review(self) -> None:
        claims = [self._claim(ClaimStatus.INSUFFICIENT_EVIDENCE, review=True)]
        report = scope_report(draft_version=1, text="текст", claims=claims)
        assert report.status is DocumentStatus.REVIEW_REQUIRED
        assert report.review_claims
