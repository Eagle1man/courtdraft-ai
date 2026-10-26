"""Четыре матрицы предмета доказывания по виду договора (пакет v1.1).

Предмет доказывания поставки (10 обстоятельств) и аренда (6) не
совпадают, поэтому одна таблица на все дела не годится: вид договора
определяется по маркерам материалов, а неподтверждённые обстоятельства
уходят в блок «Не хватает данных».
"""

from __future__ import annotations

from app.agents.evidence_pack import EvidenceItem
from app.verification.subject_of_proof import (
    DEBT_FACTS,
    LEASE_FACTS,
    LOAN_FACTS,
    MATRICES,
    SALE_FACTS,
    WORKS_FACTS,
    contract_kind,
    proof_gaps,
    subject_of_proof,
)


def _items(*texts: str) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            category="evidence",
            text=text,
            source_document=f"{n:02d}_doc.txt",
            source_type="EVIDENCE",
            order=n,
        )
        for n, text in enumerate(texts, start=1)
    ]


class TestKindDetection:
    def test_supply_case(self) -> None:
        items = _items("Договор поставки № 12 от 15.03.2026, товар.")
        assert contract_kind(items) == "купля-продажа/поставка"

    def test_loan_case(self) -> None:
        items = _items("Договор займа от 01.02.2025, расписка о получении.")
        assert contract_kind(items) == "заём"

    def test_services_case(self) -> None:
        items = _items("Договор оказания услуг, акт оказанных услуг.")
        assert contract_kind(items) == "услуги/подряд"

    def test_lease_case(self) -> None:
        items = _items("Договор аренды оборудования, арендная плата.")
        assert contract_kind(items) == "аренда"

    def test_no_markers_is_generic_debt(self) -> None:
        assert contract_kind(_items("Истец просит взыскать долг.")) == "долг"

    def test_representative_fee_does_not_beat_supply(self) -> None:
        """«Услуги представителя» встречаются в любом деле.

        Лёгкий маркер «услуг» не должен перетягивать поставочное дело
        на таблицу по услугам — для этого у маркеров разный вес.
        """
        items = _items(
            "Договор поставки, накладная на отгрузку товара. "
            "Расходы на услуги представителя 5 000 руб."
        )
        assert contract_kind(items) == "купля-продажа/поставка"

    def test_empty_pack_is_generic(self) -> None:
        assert contract_kind([]) == "долг"


class TestMatrices:
    def test_all_four_kinds_have_a_matrix(self) -> None:
        assert set(MATRICES) == {
            "купля-продажа/поставка",
            "заём",
            "услуги/подряд",
            "аренда",
            "долг",
        }

    def test_matrix_sizes_match_the_pack(self) -> None:
        assert len(SALE_FACTS) == 10
        assert len(LOAN_FACTS) == 6
        assert len(WORKS_FACTS) == 6
        assert len(LEASE_FACTS) == 6
        assert len(DEBT_FACTS) == 5

    def test_lease_table_names_the_lease_circumstances(self) -> None:
        facts = subject_of_proof(
            _items("Договор аренды помещения от 01.01.2025."),
            kind="аренда",
        )
        assert len(facts) == len(LEASE_FACTS)
        assert "аренд" in facts[0].circumstance
        assert "арендной плате" in facts[4].circumstance

    def test_supply_table_keeps_the_delivery_fact(self) -> None:
        facts = subject_of_proof(
            _items("Договор поставки № 12 от 15.03.2026."),
            kind="купля-продажа/поставка",
        )
        assert facts[1].circumstance == "передача товара"
        assert not facts[1].covered

    def test_unspecified_kind_falls_back_to_debt(self) -> None:
        facts = subject_of_proof(_items("Истец просит взыскать долг."))
        assert len(facts) == len(DEBT_FACTS)


class TestProofGaps:
    def test_uncovered_become_gap_lines(self) -> None:
        gaps = proof_gaps(_items("Истец просит взыскать долг."))
        assert gaps
        assert all("не подтверждено ни одним документом" in g for g in gaps)
        assert any("заключение договора" in g for g in gaps)

    def test_covered_case_has_no_gaps(self) -> None:
        gaps = proof_gaps(
            _items(
                "Договор, накладная на отгрузку, платёжное поручение, "
                "задолженность по акту сверки, расчёт неустойки."
            ),
            kind="долг",
        )
        assert gaps == []

    def test_kind_is_used_for_gaps(self) -> None:
        """По арендной таблице пробелы считаются по арендным фактам."""
        gaps = proof_gaps(
            _items("Договор аренды помещения от 01.01.2025."),
            kind="аренда",
        )
        assert gaps
        assert any("арендной плате" in g for g in gaps)
