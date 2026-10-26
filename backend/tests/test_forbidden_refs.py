"""Ссылки, которых в гражданском решении быть не должно.

Разбор пакета DeepSeek v1.1 (2026-10-26), блок ``forbidden_refs``.
Каждая ссылка из списка либо существует в кодексе (и потому проходит
проверку наличия статьи), либо есть в материалах дела (и потому проходит
проверку подтверждения) — оставалась дыра на «законная, но не про то».
"""

from __future__ import annotations

from app.agents.draft_agent import DraftResult, SectionDraft
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.verification.engine import IssueType, Severity, verify_draft
from app.verification.forbidden_refs import check_forbidden_refs

GOOD = (
    "В силу ст. 309 ГК РФ обязательства должны исполняться надлежащим "
    "образом. Ответчик не исполнил обязательство, и с него взыскивается "
    "задолженность. Руководствуясь ст. 194–199 ГПК РФ, суд решил:"
)


def _kinds(text: str, *, supply: bool = False) -> list[str]:
    return [f.kind for f in check_forbidden_refs(text, supply=supply)]


class TestCleanTextSilent:
    def test_good_text_has_no_findings(self) -> None:
        assert _kinds(GOOD) == []

    def test_article_310_without_penalty_is_legal(self) -> None:
        # Ст. 310 ГК РФ сама по себе законна: запрет одностороннего
        # отказа — обычный довод в споре о договоре.
        text = "Ответчик ссылается на ст. 310 ГК РФ, но договор не расторгнут."
        assert _kinds(text) == []

    def test_services_articles_in_services_case_are_legal(self) -> None:
        text = "По ст. 779 ГК РФ ответчик обязан оплатить услуги."
        assert _kinds(text, supply=False) == []


class TestForeignCode:
    """УК, УПК и КоАП в гражданском решении — критично."""

    def test_criminal_code_flagged(self) -> None:
        assert "foreign_code" in _kinds("Основание иска — ст. 161 УК РФ.")

    def test_procedural_criminal_code_flagged(self) -> None:
        assert "foreign_code" in _kinds("Суд руководствуется ст. 15 УПК РФ.")

    def test_administrative_code_flagged(self) -> None:
        assert "foreign_code" in _kinds("Основание — КоАП РФ, ст. 12.15.")

    def test_civil_laws_silent(self) -> None:
        assert "foreign_code" not in _kinds(GOOD)


class TestFormArticles:
    def test_131_132_flagged(self) -> None:
        assert "form_only" in _kinds("Суд исходит из ст. 131 ГПК РФ.")

    def test_132_flagged(self) -> None:
        assert "form_only" in _kinds("Приложения оформлены по ст. 132 ГПК РФ.")

    def test_other_gpk_articles_silent(self) -> None:
        assert "form_only" not in _kinds(
            "Срок апелляционного обжалования — ст. 321 ГПК РФ."
        )


class TestPenaltyNorm:
    def test_310_next_to_penalty_flagged(self) -> None:
        text = "Взыскать с ответчика неустойку по ст. 310 ГК РФ."
        assert "penalty_310" in _kinds(text)

    def test_330_as_penalty_basis_is_legal(self) -> None:
        text = "Взыскать неустойку на основании ст. 330 ГК РФ."
        assert "penalty_310" not in _kinds(text)


class TestServicesInSupply:
    def test_services_articles_in_supply_case_flagged(self) -> None:
        text = "Оплата взыскивается по ст. 779 ГК РФ."
        assert "services" in _kinds(text, supply=True)

    def test_781_also_flagged(self) -> None:
        text = "Оплата взыскивается по ст. 781 ГК РФ."
        assert "services" in _kinds(text, supply=True)

    def test_supply_norms_legal_in_supply_case(self) -> None:
        text = "Основание — ст. 454, 486, 506, 516 ГК РФ."
        assert "services" not in _kinds(text, supply=True)


class TestPreamble:
    def test_old_preamble_flagged(self) -> None:
        text = "Руководствуясь ст. 198 ГПК РФ, суд решил: иск удовлетворить."
        assert "preamble" in _kinds(text)

    def test_full_preamble_legal(self) -> None:
        text = "Руководствуясь ст. 194–199 ГПК РФ, суд решил: иск удовлетворить."
        assert "preamble" not in _kinds(text)

    def test_default_basis_legal(self) -> None:
        # Формула, которую система ставит по умолчанию.
        from app.drafting.operative import DEFAULT_LEGAL_BASIS

        text = f"Руководствуясь {DEFAULT_LEGAL_BASIS}, суд решил:"
        assert "preamble" not in _kinds(text)


class TestPenaltyAnd395:
    """R20: неустойка и проценты по ст. 395 ГК РФ за один период."""

    def test_awarded_together_flagged(self) -> None:
        text = (
            "Взыскать неустойку 10 000 руб. и проценты по ст. 395 ГК РФ "
            "500 руб. в пользу истца."
        )
        assert "penalty_395" in _kinds(text)

    def test_percent_denied_is_silent(self) -> None:
        text = (
            "Взыскать неустойку 10 000 руб. в пользу истца. В удовлетворении "
            "остальной части отказать: проценты по ст. 395 ГК РФ не взыскиваются."
        )
        assert "penalty_395" not in _kinds(text)

    def test_only_395_is_silent(self) -> None:
        text = "Взыскать проценты по ст. 395 ГК РФ в размере 500 руб."
        assert "penalty_395" not in _kinds(text)

    def test_penalty_alone_is_silent(self) -> None:
        text = "Взыскать неустойку в размере 10 000 руб. в пользу истца."
        assert "penalty_395" not in _kinds(text)

    def test_severity_is_medium(self) -> None:
        text = (
            "Взыскать неустойку 10 000 руб. и проценты по ст. 395 ГК РФ "
            "500 руб. в пользу истца."
        )
        found = check_forbidden_refs(text)
        assert {f.severity for f in found} == {"MEDIUM"}


class TestEngineWiring:
    """Проверка доходит до отчёта: в отчёте, а не только в функции."""

    def _report(self, text: str, *, supply: bool = False):  # noqa: ANN202
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text=text,
            is_mock=False,
            citations=["01_claim.txt"],
        )
        item = EvidenceItem(
            category="claims",
            text=(
                "Прошу взыскать задолженность по договору поставки в размере "
                "150 000 руб. 00 коп."
                if supply
                else "Прошу взыскать задолженность в размере 150 000 руб. 00 коп."
            ),
            source_document="01_claim.txt",
            source_type="иск",
            amounts=["150000"] if supply else [],
            order=1,
        )
        pack = EvidencePack(items=[item], amounts=["150000"] if supply else [])
        return verify_draft(
            DraftResult(case_number="T-0001", provider="test", sections=[section]),
            pack,
        )

    def test_foreign_code_is_critical(self) -> None:
        report = self._report("Взыскать долг со ссылкой на ст. 161 УК РФ.")
        flagged = [i for i in report.issues if i.type is IssueType.FOREIGN_CODE]
        assert flagged, [i.reason for i in report.issues]
        assert flagged[0].severity is Severity.CRITICAL

    def test_service_norm_in_supply_case_is_reported(self) -> None:
        report = self._report(
            "Взыскать 150 000 руб. по ст. 779 ГК РФ.", supply=True
        )
        flagged = [i for i in report.issues if i.type is IssueType.FORBIDDEN_REF]
        assert flagged, [i.reason for i in report.issues]
        assert any("779" in i.reason for i in flagged)

    def test_clean_operative_has_no_forbidden_refs(self) -> None:
        report = self._report(GOOD)
        assert not [
            i
            for i in report.issues
            if i.type in (IssueType.FOREIGN_CODE, IssueType.FORBIDDEN_REF)
        ]
