"""Этап 5: существование и применимость норм, матрица, согласованность."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.verification.deterministic import Finding
from app.verification.legal_refs import (
    LegalRef,
    check_applicability,
    check_existence,
    parse_refs,
)
from app.verification.matrix import build_matrix, check_consistency

LAWS = Path(__file__).resolve().parents[1] / "data" / "laws"


class TestParseRefs:
    def test_article_only(self) -> None:
        refs = parse_refs("Руководствуясь ст. 194 ГПК РФ, суд решил.")
        assert refs == [LegalRef(code="ГПК", article="194")]

    def test_point_part_article(self) -> None:
        refs = parse_refs("по п. 4 ч. 1 ст. 23 ГПК РФ")
        assert refs[0].point == "4"
        assert refs[0].part == "1"
        assert refs[0].article == "23"
        assert refs[0].code == "ГПК"

    def test_no_reference(self) -> None:
        assert parse_refs("договор займа заключён 10.02.2024") == []


class TestExistence:
    def test_existing_article(self) -> None:
        # Ст. 309 ГК РФ есть в базе законов проекта.
        assert check_existence("ссылка на ст. 309 ГК РФ", LAWS) == []

    def test_missing_article(self) -> None:
        found = check_existence("ссылка на ст. 99.99 ГК РФ", LAWS)
        assert [f.check_id for f in found] == ["LAW_NOT_FOUND"]

    def test_missing_base_is_not_silence(self) -> None:
        # Пустой каталог: норма не подтверждена, но это не повод
        # объявлять её несуществующей.
        empty = Path(__file__).parent / "_no_laws_here"
        found = check_existence("ст. 309 ГК РФ", empty)
        assert [f.check_id for f in found] == ["LAW_NOT_FOUND"]


class TestApplicability:
    def test_norm_of_services_in_supply_dispute(self) -> None:
        # Норма о возмездном оказании услуг неприменима к поставке.
        found = check_applicability("взыскать по ст. 779 ГК РФ", supply=True)
        assert found
        assert all(f.check_id == "LAW_NOT_APPLICABLE" for f in found)

    def test_same_norm_in_services_dispute(self) -> None:
        assert check_applicability("взыскать по ст. 779 ГК РФ", supply=False) == []


def _pack() -> EvidencePack:
    pack = EvidencePack(case_number="T-5", amounts=["80000", "9122.19"])
    pack.items = [
        EvidenceItem(
            category="claims",
            text="Взыскать долг 80 000 руб.",
            source_document="иск.docx",
            source_type="CLAIM",
            amounts=["80000"],
            order=1,
        ),
        EvidenceItem(
            category="objections",
            text="Ответчик просит снизить проценты.",
            source_document="возражение.docx",
            source_type="RESPONSE",
            order=2,
        ),
    ]
    return pack


class TestMatrix:
    def test_requirement_covered(self) -> None:
        rows = build_matrix(_pack(), "Взыскать долг 80 000 руб.")
        assert len(rows) == 1
        assert rows[0].covered is True
        assert rows[0].amount == Decimal("80000")
        assert "возражение.docx" in rows[0].evidence

    def test_requirement_missing_from_draft(self) -> None:
        rows = build_matrix(_pack(), "Взыскать проценты.")
        assert rows[0].covered is False


class TestConsistency:
    def test_conflicting_amounts_for_one_requirement(self) -> None:
        text = "Взыскать долг 80 000 руб. Кроме того, долг 60 000 руб."
        found = check_consistency(text)
        assert [f.check_id for f in found] == ["CONSISTENCY_AMOUNT_CONFLICT"]
        assert found[0].severity == "HIGH"

    def test_single_amount_is_consistent(self) -> None:
        assert check_consistency("Взыскать долг 80 000 руб.") == []

    def test_finding_type_is_shared(self) -> None:
        found: list[Finding] = check_consistency("долг 1 000 руб. и долг 2 000 руб.")
        assert isinstance(found[0], Finding)
