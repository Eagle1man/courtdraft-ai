"""Пороги приказного и упрощённого производства.

Ч. 1 ст. 121, п. 1 ч. 1 ст. 122 ГПК РФ — приказное до 500 000 руб.;
п. 1 ч. 1 ст. 232.2 ГПК РФ — упрощённое до 250 000 руб. (проверено по
первоисточнику 2026-10-26: порог упрощённого поднят с 100 000).
"""

from __future__ import annotations

from app.agents.draft_agent import DraftResult, SectionDraft
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.verification.engine import IssueType, Severity, verify_draft
from app.verification.procedure_limits import (
    ORDER_LIMIT,
    SIMPLIFIED_LIMIT,
    check_procedure_limits,
)


class TestThresholds:
    def test_limits_follow_the_code(self) -> None:
        assert ORDER_LIMIT == 500_000
        assert SIMPLIFIED_LIMIT == 250_000


class TestOrderProduction:
    def test_no_mention_of_order_is_silent(self) -> None:
        # Признака нет — и вопрос не задаётся: цена иска сама по себе
        # не говорит о том, что приказ вообще встречался в деле.
        # 300 000 руб. — выше порога упрощённого, чтобы проверка
        # показывала только то, о чём её спрашивают.
        assert check_procedure_limits(["Прошу взыскать долг."], 300_000) == []

    def test_order_mentioned_under_limit_reminds(self) -> None:
        texts = ["Судебный приказ от 01.03.2026 был отменён."]
        findings = check_procedure_limits(texts, 300_000)
        assert [f.kind for f in findings] == ["order"]
        assert "500 000" in findings[0].reason
        assert "повторное обращение" in findings[0].reason

    def test_order_mentioned_over_limit_flags(self) -> None:
        texts = ["В деле судебный приказ от 01.03.2026."]
        findings = check_procedure_limits(texts, 700_000)
        assert [f.kind for f in findings] == ["order_over"]
        assert "превышает порог" in findings[0].reason

    def test_zero_claims_silent(self) -> None:
        assert check_procedure_limits(["судебный приказ"], 0) == []


class TestSimplifiedProduction:
    def test_small_case_without_hearing_reminds(self) -> None:
        findings = check_procedure_limits(["Прошу взыскать долг."], 150_000)
        assert [f.kind for f in findings] == ["simplified"]
        assert "250 000" in findings[0].reason

    def test_hearing_present_is_silent(self) -> None:
        texts = ["В судебном заседании объявлен перерыв."]
        assert check_procedure_limits(texts, 150_000) == []

    def test_simplified_already_chosen_is_silent(self) -> None:
        texts = ["Дело рассматривается в упрощённом порядке."]
        assert check_procedure_limits(texts, 150_000) == []

    def test_large_case_is_silent(self) -> None:
        assert check_procedure_limits(["Прошу взыскать долг."], 300_000) == []


class TestEngineWiring:
    def test_status_reaches_report_as_low(self) -> None:
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text="Взыскать 150 000 руб. Руководствуясь ст. 194–199 ГПК РФ, суд решил:",
            is_mock=False,
            citations=["01_claim.txt"],
        )
        pack = EvidencePack(
            items=[
                EvidenceItem(
                    category="claims",
                    text=(
                        "Прошу взыскать задолженность в размере 150 000 руб. 00 коп."
                    ),
                    source_document="01_claim.txt",
                    source_type="иск",
                    amounts=["150000"],
                    order=1,
                )
            ],
            amounts=["150000"],
        )
        report = verify_draft(
            DraftResult(case_number="T-0002", provider="test", sections=[section]),
            pack,
        )
        flagged = [
            i for i in report.issues if i.type is IssueType.PROCEDURE_SIMPLIFIED
        ]
        assert flagged, [i.reason for i in report.issues]
        # Статус судье, а не дефект проекта.
        assert flagged[0].severity is Severity.LOW
