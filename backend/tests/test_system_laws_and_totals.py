"""Тест: проверка не ругается на собственную резолютивную часть.

Дефект найден на демо-стенде, который готовили к показу заказчику.
Резолютивная часть собирается по правилам, а проверка объявляла её
неподтверждённой:

    [CRITICAL] число 155100 отсутствует в материалах дела — возможна
               выдумка модели
    [HIGH]     ссылка на ст. 198 ГПК не подтверждена материалами дела
    [HIGH]     ссылка на ст. 98 ГПК не подтверждена материалами дела
    [HIGH]     ссылка на ст. 321 ГПК не подтверждена материалами дела

155100 — это 150000 + 5100, то есть «а всего» из сумм, которые в
материалах есть. Нормы цитирует сама система по закону: в иске судью
ст. 198 ГПК не пишут.

Вывод для заказчика был один: система «выдумывает» текст, который
собрала сама по правилам. Проверка при этом молчала там, где нужна:
выдумка в тексте модели по-прежнему ловится (см. последний тест).
"""

from __future__ import annotations

from app.agents.draft_agent import DraftResult, SectionDraft
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.verification.engine import Severity, VerificationReport, verify_draft

OPERATIVE = (
    "Руководствуясь ст. 194–199 ГПК РФ, суд решил:\n"
    "1. Удовлетворить исковые требования истца в полном объёме: взыскать "
    "с Фирма «Вымышленная Строй» в пользу Образцов Пётр Петрович "
    "150 000 руб. 00 коп. (задолженность по договору, 01_claim.txt).\n"
    "2. Взыскать с Фирма «Вымышленная Строй» государственную пошлину "
    "5 100 руб. 00 коп. в доход бюджета (ст. 98 ГПК РФ).\n"
    "3. Итого взыскать 155 100 руб. 00 коп.\n"
    "4. На решение может быть подана апелляционная жалоба в течение "
    "месяца (ст. 321 ГПК РФ)."
)


def _pack() -> EvidencePack:
    return EvidencePack(
        items=[
            EvidenceItem(
                category="claims",
                text=(
                    "Прошу взыскать 150 000 руб. 00 коп. "
                    "Государственную пошлину истцом уплачено 5 100 руб."
                ),
                source_document="01_claim.txt",
                source_type="иск",
                amounts=["150000", "5100"],
                order=1,
            )
        ],
        amounts=["150000", "5100"],
    )


def _report() -> object:
    section = SectionDraft(
        key="operative",
        name="РЕШИЛ",
        text=OPERATIVE,
        is_mock=False,
        citations=["01_claim.txt"],
    )
    draft = DraftResult(case_number="DEMO-001", provider="test", sections=[section])
    return verify_draft(draft, _pack())


class TestOperativeIsNotAccused:
    def test_total_not_reported_as_invented(self) -> None:
        report = _report()
        invented = [
            i.reason for i in report.issues if "отсутствует в материалах" in i.reason
        ]
        assert invented == [], f"итог объявлен выдумкой: {invented}"

    def test_no_critical_issues(self) -> None:
        report = _report()
        critical = [i for i in report.issues if i.severity is Severity.CRITICAL]
        assert critical == [], [i.reason for i in critical]

    def test_system_laws_not_flagged(self) -> None:
        """Нормы, которые цитирует система, не требуют подтверждения."""
        report = _report()
        flagged = [i.reason for i in report.issues if "не подтверждена" in i.reason]
        assert flagged == [], flagged


class TestRulesBuiltSectionsNotAccused:
    """Разделы правил не обвиняются в выдумке модели.

    Живой прогон 22.10.2026 (real-04): итог резолюции 32 692 руб.
    60 коп. — сумма четырёх требований плюс иные расходы, копейки
    перенесены из требований. Такого числа в материалах в готовом
    виде быть не может, и проверка помечала его выдумкой модели:
    8 CRITICAL на проекте без единой выдумки, а настоящие тревоги в
    этом шуме не видно.
    """

    TOTAL_SECTION = (
        "Руководствуясь ст. 194–199 ГПК РФ, суд решил:\n"
        "1. Удовлетворить исковые требования истца в полном объёме: "
        "взыскать с Ответчик в пользу Истец 10 490 руб. 00 коп. "
        "(ущерб, 01_claim.txt).\n"
        "2. Взыскать 32 692 руб. 60 коп. всего (итог проекта)."
    )

    def _report_with(self, *, rules: bool) -> VerificationReport:
        pack = EvidencePack(
            items=[
                EvidenceItem(
                    category="claims",
                    text=(
                        "Прошу взыскать 10 490 руб. и 2 202 руб. 90 коп. "
                        "и 10 000 руб. и 7 000 руб."
                    ),
                    source_document="01_claim.txt",
                    source_type="иск",
                    amounts=["10490", "2202", "10000", "7000"],
                    order=1,
                )
            ],
            amounts=["10490", "2202", "10000", "7000"],
        )
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text=self.TOTAL_SECTION,
            is_mock=False,
            citations=["01_claim.txt"],
            **({"built_by": "rules"} if rules else {}),
        )
        return verify_draft(
            DraftResult(case_number="X", provider="test", sections=[section]),
            pack,
        )

    def test_rules_total_not_accused(self) -> None:
        critical = [
            i.reason
            for i in self._report_with(rules=True).issues
            if i.severity is Severity.CRITICAL
        ]
        assert critical == [], critical

    def test_model_total_still_accused(self) -> None:
        """Ослабления для модели нет: та же фраза от модели — тревога."""
        critical = [
            i.reason
            for i in self._report_with(rules=False).issues
            if i.severity is Severity.CRITICAL
        ]
        assert critical, "выдуманное число пропущено"


class TestExemptionsAreNarrow:
    def test_law_from_nothing_still_flagged(self) -> None:
        """Норму, которой нет нигде, по-прежнему ловим.

        Ослабление проверки на две конкретные вещи — арифметику и
        собственные нормы — не должно превращаться в «нормы не
        проверяются вообще».
        """
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text="Руководствуясь ст. 719 ГК РФ, суд решил взыскать 300 000 руб.",
            is_mock=False,
            citations=["01_claim.txt"],
        )
        draft = DraftResult(
            case_number="DEMO-001", provider="test", sections=[section]
        )
        report = verify_draft(draft, _pack())
        assert any("719" in i.reason for i in report.issues), [
            i.reason for i in report.issues
        ]

    def test_random_number_still_critical(self) -> None:
        """Число, не равное сумме подтверждённых, — по-прежнему CRITICAL."""
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text="Взыскать 987 654 руб. 00 коп. (01_claim.txt).",
            is_mock=False,
            citations=["01_claim.txt"],
        )
        draft = DraftResult(
            case_number="DEMO-001", provider="test", sections=[section]
        )
        report = verify_draft(draft, _pack())
        critical = [i for i in report.issues if i.severity is Severity.CRITICAL]
        assert critical, "выдуманное число пропущено"

    def test_sum_of_unconfirmed_numbers_is_not_excused(self) -> None:
        """Сложить два неподтверждённых числа недостаточно.

        Иначе модель, выдумав 500000 и прибавив 5100, получила бы
        «подтверждённый» итог 505100.
        """
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text="Взыскать 500 000 руб. и 505 100 руб. (01_claim.txt).",
            is_mock=False,
            citations=["01_claim.txt"],
        )
        draft = DraftResult(
            case_number="DEMO-001", provider="test", sections=[section]
        )
        report = verify_draft(draft, _pack())
        critical = [i.reason for i in report.issues if i.severity is Severity.CRITICAL]
        assert len(critical) == 2, [i.reason for i in critical]

    def test_single_number_is_not_a_sum(self) -> None:
        """Сумма из одного слагаемого — не оправдание."""
        section = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text="Взыскать 5100 руб. дважды подряд.",
            is_mock=False,
            citations=[],
        )
        draft = DraftResult(
            case_number="DEMO-001", provider="test", sections=[section]
        )
        report = verify_draft(draft, _pack())
        # 5100 в материалах есть, поэтому подтверждается напрямую.
        assert not [
            i for i in report.issues if i.severity is Severity.CRITICAL
        ]
