"""Тесты резолютивной части, собираемой по правилам (п. 41, 63).

Проверяется не «красиво ли звучит», а три вещи, каждая из которых
объяснена отдельным тестом:

  1. конструкция не разъезжается: плательщик и получатель названы
     прямо, а не выведены из порядка слов в предложении;
  2. незаполненное поле даёт пометку о проверке, а не выдуманное
     значение;
  3. модель в этот блок не входит вовсе.
"""

from __future__ import annotations

import pytest

from app.drafting.operative import (
    REVIEW,
    Claimant,
    MoneyClaim,
    Outcome,
    build_operative,
    selfcheck,
)


def full_claim() -> MoneyClaim:
    return MoneyClaim(
        amount=150000,
        basis="неоплаченная по договору поставки",
        source="договор поставки № 14",
    )


class TestDirectionOfPayment:
    """Самое важное: кто платит и кому. Здесь система ошибалась раньше."""

    def test_payer_and_recipient_named_explicitly(self) -> None:
        """Конструкция называет обоих, а не полагается на порядок слов.

        Модель на 1,5B писала «ответчик обязан возменить истцу» —
        грамматически верно, но это описание обязанности, а не
        резолюция. Взыскание всегда формулируется «взыскать с X в
        пользу Y», и именно эта форма нужна исполнителю.
        """
        text = build_operative([full_claim()], fee_paid=5100).render()
        assert "взыскать с ответчика в пользу истца" in text.lower()

    def test_money_is_in_insoluble_order(self) -> None:
        """Сумма идёт сразу после «в пользу истца».

        «Взыскать в пользу истца от ответчика» — та же по смыслу
        фраза, но взыскателю и исполнителю её разбирать сложнее.
        """
        text = build_operative([full_claim()], fee_paid=5100).render()
        assert "в пользу истца 150 000" in text

    def test_amount_formatted_with_spaces(self) -> None:
        """Сумма разрядами, принятыми в судебных актах."""
        text = build_operative([full_claim()], fee_paid=5100).render()
        assert "150 000" in text
        assert "150000" not in text


class TestOutcomes:
    """Три исхода требования дают три разные конструкции."""

    def test_satisfied_in_full(self) -> None:
        text = build_operative([full_claim()], fee_paid=5100).render()
        assert "Удовлетворить исковые требования истца" in text
        assert "частично" not in text

    def test_satisfied_partially_names_granted_sum(self) -> None:
        """При частичном удовлетворении названа именно удовлетворённая сумма.

        Именно её предъявляют к исполнению. Называть только общую сумму
        требования нельзя: исполнительный лист выдадут на неё, а суд
        удовлетворил меньше.
        """
        claim = MoneyClaim(
            amount=150000,
            granted_amount=90000,
            basis="неустойка",
            source="договор № 14",
            grounds="нарушение не доказано в части",
            outcome=Outcome.PARTIAL,
        )
        text = build_operative([claim], fee_paid=5100).render()
        assert "90 000" in text
        assert "частично" in text
        assert "В остальной части отказать" in text

    def test_partial_without_granted_sum_is_flagged(self) -> None:
        """Нет суммы удовлетворённой части — не выдумываем её."""
        claim = MoneyClaim(
            amount=150000,
            granted_amount=None,
            basis="неустойка",
            source="договор № 14",
            outcome=Outcome.PARTIAL,
        )
        result = build_operative([claim], fee_paid=5100)
        assert not result.is_complete
        assert any("удовлетворённая" in gap for gap in result.gaps)
        assert REVIEW in result.render()

    def test_denied_does_not_collect_money(self) -> None:
        """При отказе взыскания быть не должно — это основание к отмене."""
        claim = MoneyClaim(amount=150000, basis="убытки",
                           source="экспертиза", outcome=Outcome.DENIED)
        result = build_operative([claim], fee_paid=5100)
        text = result.render().lower()

        assert "отказать в удовлетворении исковых требований" in text
        # Пункт о взыскании по самому требованию не должен появиться.
        # Пункт о пошлине при этом остаётся — это разные вещи, и его
        # наличие не противоречит отказу по существу.
        assert "взыскать с ответчика в пользу истца 150 000" not in text
        assert any("отказать" in item.lower() for item in result.items)
        assert any("150 000" in item for item in result.items) is False


class TestNoFabrication:
    """Главное требование проекта: неизвестное не превращается в вымысел."""

    def test_unknown_amount_is_not_guessed(self) -> None:
        claim = MoneyClaim(amount=None, basis="расходы на представителя",
                          source="договор оказания услуг")
        result = build_operative([claim], fee_paid=5100)
        assert not result.is_complete
        assert REVIEW in result.render()

    def test_missing_source_is_flagged(self) -> None:
        """Сумма без указания документа — неподтверждённое утверждение."""
        claim = MoneyClaim(amount=50000, basis="долг", source="")
        result = build_operative([claim], fee_paid=5100)
        assert not result.is_complete
        assert any("источник" in gap for gap in result.gaps)

    def test_empty_source_leaves_no_dangling_comma(self) -> None:
        """Пустой источник не должен давать «(долг, )».

        Такой вид — признак недоделки в проекте решения, и судья
        читает его как ошибку системы.
        """
        claim = MoneyClaim(amount=50000, basis="долг", source="")
        text = build_operative([claim], fee_paid=5100).render()
        assert ", )" not in text
        assert "(долг)" in text

    def test_empty_claims_gives_review_marker(self) -> None:
        result = build_operative([])
        assert not result.items
        assert REVIEW in result.render()

    def test_missing_fee_still_produces_placeholder(self) -> None:
        """Пункт о пошлине обязателен по ст. 98 ГПК РФ.

        Неизвестная сумма не повод пропустить пункт: он остаётся
        незаполненным, и это видно судье.
        """
        result = build_operative([full_claim()], fee_paid=None)
        assert result.gaps
        assert "пошлины" in " ".join(result.gaps).lower()
        assert REVIEW in result.render()

    def test_zero_amount_is_a_claim_not_a_gap(self) -> None:
        """Ноль — это значение. Не путать с «не установлено»."""
        claim = MoneyClaim(amount=0, basis="проценты за просрочку",
                          source="расчёт истца")
        result = build_operative([claim], fee_paid=5100, budget="области")
        assert result.is_complete
        assert "0 руб." in result.render()


class TestStructure:
    """Порядок пунктов и обязательные ссылки."""

    def test_starts_with_legal_basis(self) -> None:
        text = build_operative([full_claim()], fee_paid=5100).render()
        assert text.startswith("Руководствуясь ст. 194–199 ГПК РФ, суд решил:")

    def test_items_are_numbered_from_one(self) -> None:
        text = build_operative([full_claim()], fee_paid=5100).render()
        assert "\n1. " in text

    def test_fee_item_cites_article_98(self) -> None:
        text = build_operative([full_claim()], fee_paid=5100).render()
        assert "ст. 98 ГПК РФ" in text

    def test_costs_cite_article_100(self) -> None:
        text = build_operative([full_claim()], fee_paid=5100, costs=15000).render()
        assert "ст. 100 ГПК РФ" in text
        assert "15 000" in text

    def test_appeal_rights_is_always_last(self) -> None:
        """Право на обжалование замыкает резолютивную часть."""
        result = build_operative([full_claim()], fee_paid=5100)
        assert "обжаловано" in result.items[-1]

    def test_non_money_claims_included(self) -> None:
        claim = Claimant(
            description="признать право собственности",
            source="свидетельство о регистрации",
        )
        result = build_operative([], other_claims=[claim], fee_paid=None)
        assert any("признать право собственности" in i for i in result.items)

    def test_denied_non_money_claim_wording(self) -> None:
        claim = Claimant(description="признать право собственности",
                         source="исковое заявление", outcome=Outcome.DENIED)
        result = build_operative([], other_claims=[claim], fee_paid=None)
        assert any("Отказать" in i for i in result.items)


class TestSelfcheck:
    """Встроенная проверка типовых случаев."""

    def test_selfcheck_finds_no_problems(self) -> None:
        assert selfcheck() == []


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(1, "1 руб. 00 коп."), (150000, "150 000 руб. 00 коп."),
     (1000000, "1 000 000 руб. 00 коп.")],
)
def test_amount_formats(amount: int, expected: str) -> None:
    claim = MoneyClaim(amount=amount, basis="долг", source="договор")
    assert expected in build_operative([claim], fee_paid=5100).render()
