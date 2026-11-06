"""Проверка цитат и противоречий (этап 3).

Ключевые требования ТЗ §7.2: различать дословное совпадение, совпадение
после нормализации и отсутствие; не выдавать факт за подтверждённый без
найденной цитаты. §7.4: расхождения сохраняются, а не «исправляются»
выбором одной версии.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.extraction.schemas import (
    CaseAmount,
    CaseDate,
    CaseFact,
    CaseRegistry,
    ExtractionStatus,
    FactStatus,
    SourceRef,
)
from app.validation.evidence import (
    EvidenceIssueType,
    QuoteStatus,
    apply_contradictions,
    check_quote,
    detect_contradictions,
    normalize,
    verify_registry,
)

TEXT = (
    "Истец ООО «Ромашка» просит взыскать 1 000,00 руб. "
    "Договор поставки заключён 15.01.2025."
)


def _fact(quote: str, *, fact_id: str = "F-1") -> CaseFact:
    return CaseFact(
        fact_id=fact_id,
        fact_type="contract",
        value="Заключён договор поставки",
        source=SourceRef(document_id="DOC-1", quote=quote),
    )


def _registry(*facts: CaseFact, amounts: list[CaseAmount] | None = None,
              dates: list[CaseDate] | None = None) -> CaseRegistry:
    return CaseRegistry(
        facts=list(facts),
        amounts=amounts or [],
        dates=dates or [],
    )


class TestQuoteCheck:
    def test_exact_quote(self) -> None:
        result = check_quote("Договор поставки заключён", TEXT)

        assert result.status is QuoteStatus.EXACT
        assert result.start is not None

    def test_normalised_quote(self) -> None:
        # Другая ёлочка, лишние пробелы и ё вместо е — это тот же текст.
        result = check_quote('Истец  ООО "Ромашка"  просит', TEXT)

        assert result.status is QuoteStatus.NORMALIZED

    def test_absent_quote(self) -> None:
        result = check_quote("Ответчик признал долг полностью", TEXT)

        assert result.status is QuoteStatus.ABSENT

    def test_too_short_quote_is_absent(self) -> None:
        assert check_quote("ООО", TEXT).status is QuoteStatus.ABSENT

    def test_normalisation_does_not_touch_words(self) -> None:
        assert normalize("Договор — «Поставки» Ёлка") == 'договор - "поставки" елка'


class TestVerifyRegistry:
    def test_fact_with_exact_quote_is_verified(self) -> None:
        registry = _registry(_fact("Договор поставки заключён"))

        report = verify_registry(registry, {"DOC-1": TEXT})

        assert registry.facts[0].status is FactStatus.VERIFIED
        assert report.exact == 1
        assert report.status is ExtractionStatus.OK

    def test_fact_with_normalised_quote_is_matched_not_verified(self) -> None:
        registry = _registry(_fact('Истец  ООО "Ромашка"'))

        report = verify_registry(registry, {"DOC-1": TEXT})

        assert registry.facts[0].status is FactStatus.SOURCE_MATCHED
        assert report.normalized == 1
        assert any(i.type is EvidenceIssueType.QUOTE_NORMALIZED for i in report.issues)

    def test_fact_with_absent_quote_is_unsupported(self) -> None:
        registry = _registry(_fact("Ответчик полностью признал иск"))

        report = verify_registry(registry, {"DOC-1": TEXT})

        assert registry.facts[0].status is FactStatus.UNSUPPORTED
        assert report.absent == 1
        assert any(i.type is EvidenceIssueType.QUOTE_ABSENT for i in report.issues)

    def test_fact_without_source_is_unsupported(self) -> None:
        registry = _registry(_fact(""))

        report = verify_registry(registry, {"DOC-1": TEXT})

        assert registry.facts[0].status is FactStatus.UNSUPPORTED
        assert any(i.type is EvidenceIssueType.SOURCE_MISSING for i in report.issues)

    def test_missing_document_blocks(self) -> None:
        registry = _registry(_fact("Договор поставки заключён"))

        report = verify_registry(registry, {})

        assert report.status is ExtractionStatus.NEEDS_REVIEW
        assert registry.status is ExtractionStatus.NEEDS_REVIEW

    def test_amount_quote_is_checked_too(self) -> None:
        registry = _registry(
            amounts=[
                CaseAmount(
                    kind="principal",
                    value=Decimal("1000.00"),
                    source=SourceRef(document_id="DOC-1", quote="выдуманная цитата"),
                )
            ]
        )

        report = verify_registry(registry, {"DOC-1": TEXT})

        assert any(i.type is EvidenceIssueType.QUOTE_ABSENT for i in report.issues)


class TestContradictions:
    def test_different_amounts_of_same_kind_are_blocking(self) -> None:
        registry = _registry(
            amounts=[
                CaseAmount(kind="principal", value=Decimal("1000.00")),
                CaseAmount(kind="principal", value=Decimal("1500.00")),
            ]
        )

        found = apply_contradictions(registry)

        assert len(found) == 1
        assert found[0].blocking is True
        assert found[0].values == ["1000.00", "1500.00"]
        assert registry.status is ExtractionStatus.NEEDS_REVIEW

    def test_same_amounts_do_not_conflict(self) -> None:
        registry = _registry(
            amounts=[
                CaseAmount(kind="principal", value=Decimal("1000.00")),
                CaseAmount(kind="principal", value=Decimal("1000.00")),
            ]
        )

        assert detect_contradictions(registry) == []

    def test_same_amount_in_different_notation_is_not_a_conflict(self) -> None:
        registry = _registry(
            amounts=[
                CaseAmount(kind="principal", value=Decimal("1000")),
                CaseAmount(kind="principal", value=Decimal("1000.00")),
            ]
        )

        assert detect_contradictions(registry) == []

    def test_different_dates_are_not_blocking(self) -> None:
        registry = _registry(
            dates=[
                CaseDate(kind="contract", value=date(2025, 1, 15)),
                CaseDate(kind="contract", value=date(2024, 12, 1)),
            ]
        )

        found = detect_contradictions(registry)

        assert len(found) == 1
        assert found[0].blocking is False

    def test_different_kinds_do_not_conflict(self) -> None:
        registry = _registry(
            amounts=[
                CaseAmount(kind="principal", value=Decimal("1000.00")),
                CaseAmount(kind="penalty", value=Decimal("15.50")),
            ]
        )

        assert detect_contradictions(registry) == []
