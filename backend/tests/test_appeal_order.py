"""R22: порядок обжалования должен соответствовать суду и производству.

Срок и инстанция обжалования — не общая формула: мировой судья отправляет
жалобу в районный суд (ст. 320.1 ГПК РФ), упрощённый порядок даёт 15 дней
вместо месяца (ст. 232.4 ГПК РФ). Пакет v1.1, self_check R22.
"""

from __future__ import annotations

from app.agents.draft_agent import DraftResult, SectionDraft
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.verification.engine import IssueType, Severity, verify_draft
from app.verification.resol_checks import check_resolution

WITHOUT_COURT = (
    "Взыскать с ООО «СтройМир» (ИНН: 7712345678) в пользу Ковалёва "
    "Ильи Сергеевича (паспорт: 45 09 112233) задолженность в размере "
    "35 000,00 руб. Решение может быть обжаловано в течение месяца со "
    "дня принятия решения суда в окончательной форме."
)

WITH_COURT = (
    "Взыскать с ООО «СтройМир» (ИНН: 7712345678) в пользу Ковалёва "
    "Ильи Сергеевича (паспорт: 45 09 112233) задолженность в размере "
    "35 000,00 руб. Решение может быть обжаловано в Промышленный районный "
    "суд г. Самары через мирового судью судебного участка № 5 в течение "
    "месяца со дня принятия решения суда в окончательной форме."
)

FIFTEEN_DAYS = (
    "Взыскать с ООО «СтройМир» (ИНН: 7712345678) в пользу Ковалёва "
    "Ильи Сергеевича (паспорт: 45 09 112233) задолженность в размере "
    "35 000,00 руб. Решение может быть обжаловано в Промышленный районный "
    "суд г. Самары в течение пятнадцати дней со дня вынесения решения."
)


def _ids(text: str, meta: dict[str, object]) -> set[str]:
    return {d.id for d in check_resolution(text, meta)}


class TestAppellateCourt:
    def test_world_court_without_named_instance(self) -> None:
        found = _ids(WITHOUT_COURT, {"world_court": True})
        assert "RESOL_APPEAL_COURT" in found

    def test_world_court_with_named_instance_silent(self) -> None:
        found = _ids(WITH_COURT, {"world_court": True})
        assert "RESOL_APPEAL_COURT" not in found

    def test_district_court_case_silent(self) -> None:
        # Флаг не выставлен — правило молчит, а не угадывает суд.
        found = _ids(WITHOUT_COURT, {"world_court": False})
        assert "RESOL_APPEAL_COURT" not in found

    def test_no_appeal_paragraph_is_not_this_rule(self) -> None:
        text = "Взыскать 35 000,00 руб. в пользу истца."
        found = _ids(text, {"world_court": True})
        assert "RESOL_APPEAL_COURT" not in found
        assert "RESOL_NO_APPEAL" in found


class TestSimplifiedTerm:
    def test_month_in_simplified_case_flagged(self) -> None:
        found = _ids(WITH_COURT, {"simplified": True})
        assert "RESOL_APPEAL_TERM" in found

    def test_fifteen_days_is_correct(self) -> None:
        found = _ids(FIFTEEN_DAYS, {"simplified": True})
        assert "RESOL_APPEAL_TERM" not in found

    def test_general_order_month_is_correct(self) -> None:
        found = _ids(WITH_COURT, {"simplified": False})
        assert "RESOL_APPEAL_TERM" not in found


class TestEngineWiring:
    """Метаданные берутся из материалов: флаг не надо вводить руками."""

    def test_court_from_documents_reaches_the_rule(self) -> None:
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text=WITHOUT_COURT,
            is_mock=False,
            citations=["01_claim.txt"],
        )
        pack = EvidencePack(
            case_number="T-0003",
            court_from_docs=(
                "Мировой судья судебного участка № 5 Промышленного "
                "судебного района г. Самары"
            ),
            items=[
                EvidenceItem(
                    category="claims",
                    text=(
                        "Прошу взыскать задолженность в размере 35 000 руб. 00 коп."
                    ),
                    source_document="01_claim.txt",
                    source_type="иск",
                    amounts=["35000"],
                    order=1,
                )
            ],
            amounts=["35000"],
        )
        report = verify_draft(
            DraftResult(case_number="T-0003", provider="test", sections=[section]),
            pack,
        )
        flagged = [i for i in report.issues if i.type is IssueType.RESOL_APPEAL_COURT]
        assert flagged, [i.reason for i in report.issues]
        assert flagged[0].severity is Severity.MEDIUM
        assert "320.1" in flagged[0].reason

    def test_no_court_in_materials_is_silent(self) -> None:
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text=WITHOUT_COURT,
            is_mock=False,
            citations=["01_claim.txt"],
        )
        pack = EvidencePack(case_number="T-0004")
        report = verify_draft(
            DraftResult(case_number="T-0004", provider="test", sections=[section]),
            pack,
        )
        assert not [
            i for i in report.issues if i.type is IssueType.RESOL_APPEAL_COURT
        ]
