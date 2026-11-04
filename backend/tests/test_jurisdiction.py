"""Вид судопроизводства: определение по материалам, шаблон и проверка."""

from __future__ import annotations

from app.agents.draft_agent import DraftResult, SectionDraft
from app.agents.evidence_pack import EvidencePack, template_name_for_case
from app.ingestion.jurisdiction import (
    ARBITRATION,
    CIVIL,
    detect_jurisdiction,
)
from app.verification.engine import IssueType, verify_draft


class TestDetect:
    def test_arbitration_by_court_header(self) -> None:
        verdict = detect_jurisdiction([
            "В Арбитражный суд города Москвы\nИстец: ООО «Ромашка»"
        ])
        assert verdict.case_type == ARBITRATION
        assert verdict.confidence in {"medium", "high"}
        assert verdict.reasons

    def test_arbitration_by_case_number(self) -> None:
        verdict = detect_jurisdiction(["Дело № А40-12345/2026"])
        assert verdict.case_type == ARBITRATION

    def test_arbitration_by_apk_reference(self) -> None:
        verdict = detect_jurisdiction([
            "Иск подан в порядке ст. 125 АПК РФ. Ответчик: ООО «Вектор»."
        ])
        assert verdict.case_type == ARBITRATION

    def test_civil_by_district_court(self) -> None:
        verdict = detect_jurisdiction([
            "Пресненский районный суд г. Москвы\nИстец: Образцов Пётр Петрович"
        ])
        assert verdict.case_type == CIVIL

    def test_world_judge_is_civil(self) -> None:
        verdict = detect_jurisdiction([
            "Мировой судья судебного участка № 123 рассматривает дело"
        ])
        assert verdict.case_type == CIVIL

    def test_gpk_reference_is_civil(self) -> None:
        verdict = detect_jurisdiction(["Иск подан по правилам ст. 131 ГПК РФ"])
        assert verdict.case_type == CIVIL

    def test_empty_when_no_signs(self) -> None:
        verdict = detect_jurisdiction(["Договор поставки № 7 от 01.02.2025"])
        assert verdict.case_type == ""
        assert not verdict.detected

    def test_contradicting_signs_do_not_decide(self) -> None:
        # По одному сильному признаку с каждой стороны: угадывать нельзя.
        verdict = detect_jurisdiction([
            "Арбитражный суд рассмотрел спор",
            "Решение вынесено по нормам ГПК РФ",
        ])
        assert verdict.case_type == ""

    def test_both_sides_business_tips_arbitration(self) -> None:
        verdict = detect_jurisdiction(
            ["Спор из договора поставки, ИНН 7701234567"],
            plaintiff="ООО «Ромашка»",
            defendant="ООО «Вектор»",
        )
        assert verdict.case_type == ARBITRATION

    def test_empty_texts(self) -> None:
        assert detect_jurisdiction([]).case_type == ""
        assert detect_jurisdiction(["", None]).case_type == ""  # type: ignore[list-item]


class TestTemplateChoice:
    def test_arbitration_uses_arbitration_template(self) -> None:
        assert template_name_for_case("arbitration") == "arbitration_judgment"

    def test_civil_uses_civil_template(self) -> None:
        assert template_name_for_case("civil") == "civil_judgment"

    def test_unknown_falls_back_to_civil(self) -> None:
        assert template_name_for_case("") == "civil_judgment"
        assert template_name_for_case("что-то") == "civil_judgment"


def _draft(text: str) -> DraftResult:
    return DraftResult(
        case_number="А40-1/2026",
        sections=[SectionDraft(key="motivating", name="Мотивировочная", text=text)],
    )


class TestCodecMismatch:
    def test_arbitration_case_with_gpk_is_flagged(self) -> None:
        pack = EvidencePack(case_number="А40-1/2026", jurisdiction="arbitration")
        report = verify_draft(_draft("Суд применяет ст. 198 ГПК РФ."), pack)
        assert any(i.type == IssueType.JURISDICTION_MISMATCH for i in report.issues)

    def test_civil_case_with_apk_is_flagged(self) -> None:
        pack = EvidencePack(case_number="2-1/2026", jurisdiction="civil")
        report = verify_draft(_draft("Суд применяет ст. 168 АПК РФ."), pack)
        assert any(i.type == IssueType.JURISDICTION_MISMATCH for i in report.issues)

    def test_negated_mentions_are_not_flagged(self) -> None:
        pack = EvidencePack(case_number="А40-1/2026", jurisdiction="arbitration")
        report = verify_draft(
            _draft("Нормы ГПК РФ к арбитражному делу не применяются."), pack
        )
        assert not any(i.type == IssueType.JURISDICTION_MISMATCH for i in report.issues)

    def test_unknown_jurisdiction_is_not_checked(self) -> None:
        pack = EvidencePack(case_number="1/2026", jurisdiction="")
        report = verify_draft(_draft("Суд применяет ст. 198 ГПК РФ."), pack)
        assert not any(i.type == IssueType.JURISDICTION_MISMATCH for i in report.issues)
