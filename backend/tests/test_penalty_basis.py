"""PENALTY_BASIS_UNCONFIRMED: штраф в резолютивной против материалов дела.

Штраф (п. 6 ст. 13 ЗоЗПП) возможен только при отказе добровольно
удовлетворить претензию; отказа в материалах не видно — но не найдено
не равно не было, поэтому отсутствие претензии понижено до MEDIUM.
"""

from __future__ import annotations

from app.agents.evidence_pack import EvidencePack
from app.verification.engine import (
    IssueType,
    Severity,
    VerificationReport,
    _check_penalty_basis,
)

OPERATIVE = (
    "Взыскать с ООО «Торговый дом» в пользу Ивановой Марии Петровны "
    "штраф в размере 6 495,00 руб. Решение может быть обжаловано "
    "в Ленинский районный суд через мирового судью в течение месяца."
)
CLAIM = "Претензия от 01.09.2025. Прошу вернуть деньги за товар ненадлежащего качества."


def _pack(documents: dict[str, str]) -> EvidencePack:
    return EvidencePack(documents=documents)


def _check(text: str, pack: EvidencePack) -> VerificationReport:
    report = VerificationReport()
    _check_penalty_basis(text, pack, report, "Резолютивная часть")
    return report


class TestPenaltyBasis:
    """Основания штрафа сверяются с материалами дела."""

    def test_no_penalty_is_silent(self) -> None:
        report = _check("Взыскать неустойку в размере 5 000,00 руб.", _pack({}))
        assert report.issues == []

    def test_penalty_without_claim_is_medium(self) -> None:
        report = _check(OPERATIVE, _pack({"01_исковое.txt": "Прошу взыскать штраф."}))
        [issue] = report.issues
        assert issue.type == IssueType.PENALTY_BASIS_UNCONFIRMED
        assert issue.severity == Severity.MEDIUM
        assert "претензия не найдена" in issue.reason

    def test_penalty_with_timely_satisfaction_is_high(self) -> None:
        # Исполнено 05.09 — через четыре дня после претензии: в срок,
        # отказа добровольно не удовлетворять нет.
        report = _check(
            OPERATIVE,
            _pack(
                {
                    "01_претензия.txt": CLAIM,
                    "02_оплата.txt": "Оплата произведена 05.09.2025.",
                }
            ),
        )
        [issue] = report.issues
        assert issue.type == IssueType.PENALTY_BASIS_UNCONFIRMED
        assert issue.severity == Severity.HIGH
        assert "подтверждённого отказа" in issue.reason

    def test_penalty_with_late_satisfaction_is_silent(self) -> None:
        # Исполнено 01.10 — через 30 дней: десятидневный срок вышел,
        # отказ добровольного удовлетворения считается подтверждённым.
        report = _check(
            OPERATIVE,
            _pack(
                {
                    "01_претензия.txt": CLAIM,
                    "02_оплата.txt": "Оплата произведена 01.10.2025.",
                }
            ),
        )
        assert report.issues == []

    def test_claim_without_dates_is_silent(self) -> None:
        # Без даты претензии срок не посчитать — проверка молчит, а не угадывает.
        report = _check(
            OPERATIVE,
            _pack({"01_претензия.txt": "Претензия. Прошу вернуть деньги."}),
        )
        assert report.issues == []
