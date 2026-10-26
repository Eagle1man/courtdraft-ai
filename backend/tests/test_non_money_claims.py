"""Неимущественные требования должны доходить до резолютивной части.

В проекте решения резолюция обязана разрешить каждое заявленное
требование, а не только денежные. Расторжение договора — самое частое
неимущественное требование в договорных спорах, и именно оно терялось:
формулировки «расторгнуть договор» и «отказаться от исполнения договора»
не распознавались, требование уходило в список неопознанных, а в деле
real-04 в проекте решения расторжения не было вовсе.

Проверяется не только распознавание, но и то, что требование дошло до
текста резолюции: нераспознанное требование для судьи выглядит так,
будто его и не заявляли.
"""

from __future__ import annotations

from app.agents.claim_phrases import claim_basis
from app.drafting.operative import Claimant, Outcome, build_operative

TERMINATION = [
    "Истец просит расторгнуть договор поставки № 14.",
    "Прошу расторгнуть договор аренды нежилого помещения.",
    "Требование о расторжении договора подлежит удовлетворению.",
]

REFUSAL_TO_PERFORM = [
    "Истец заявляет отказ от исполнения договора поставки.",
    "Прошу отказаться от исполнения договора в одностороннем порядке.",
    "Отказ от исполнения договора заявлен истцом письменно.",
]


class TestTerminationRecognised:
    """Расторжение договора — самостоятельное требование."""

    def test_formulations_recognised(self) -> None:
        for text in TERMINATION:
            assert claim_basis(text) == "расторжение договора", text

    def test_reaches_operative_part(self) -> None:
        """Требование обязано появиться в проекте решения."""
        claim = Claimant(
            description="расторжение договора",
            source="исковое заявление",
            outcome=Outcome.SATISFIED,
        )
        result = build_operative([], [claim])
        assert any("расторжение договора" in item for item in result.items)

    def test_source_kept_out_of_text(self) -> None:
        """Имя источника не попадает в текст акта.

        Раньше пункт кончался «(расторжение договора, иск.docx)»: имя
        файла в решении суда читается как техническая ошибка. Источник
        остаётся в citations раздела и судья видит его в интерфейсе, в
        текст пункта он не превращается.
        """
        claim = Claimant(
            description="расторжение договора",
            source="исковое заявление",
            outcome=Outcome.SATISFIED,
        )
        result = build_operative([], [claim])
        assert any("расторжение договора" in item for item in result.items)
        assert all("исковое заявление" not in item for item in result.items)


class TestRefusalToPerformRecognised:
    """Отказ от исполнения договора — тоже требование."""

    def test_formulations_recognised(self) -> None:
        for text in REFUSAL_TO_PERFORM:
            assert claim_basis(text) == "отказ от исполнения договора", text


class TestDenialRendering:
    """Отказ по неимущественному требованию должен выражаться явно."""

    def test_denied_is_rendered(self) -> None:
        claim = Claimant(
            description="расторжение договора",
            source="исковое заявление",
            outcome=Outcome.DENIED,
        )
        result = build_operative([], [claim])
        assert any("Отказать" in item for item in result.items)

    def test_missing_source_is_flagged(self) -> None:
        """Требование без источника — повод для проверки, а не молчание."""
        claim = Claimant(description="расторжение договора", source="")
        result = build_operative([], [claim])
        assert any("источник" in gap for gap in result.gaps)
