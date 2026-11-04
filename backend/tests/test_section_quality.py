"""Тест: рассуждения модели не попадают в проект решения.

Дефект найден на демонстрации: описательная часть решения пришла
английским рассуждением —

    Okay, let's tackle this. The user wants the "Описательная часть"...
    Wait, the materials mention the defendant's objection...
    Since the materials don't specify the contract terms

— 1601 латинская буква против 70 кириллических. Такой текст стоял в
проекте решения, который показывается заказчику.

Молча выбросить его нельзя: судья увидит в проекте чужую болтовню и
решит, что система не работает. Раздел помечается как не
сформированный, с указанием причины.
"""

from __future__ import annotations

import pytest

from app.drafting.section_quality import (
    motivating_shortcoming,
    not_formed_text,
    unusable_reason,
)

REASONING = (
    "Okay, let's tackle this. The user wants the \"описательная часть\" of "
    "the court decision. The key points are the circumstances and evidence "
    "provided in the materials.\n\n"
    "First, I need to look through the materials listed. The main claims "
    "are about recovering 150,000 rubles from the defendant, and the court "
    "has to decide on the validity of these claims.\n\n"
    "Wait, the materials mention the defendant's objection about the "
    "product's quality, but the evidence shows the plaintiff's claim about "
    "the debt.\n\n"
    "Since the materials don't specify the contract terms, the court might "
    "need to check if the product's defect is a valid counterclaim."
)

RUSSIAN = (
    "Истец обратился в суд с требованием о взыскании задолженности по "
    "договору поставки в размере 150 000 рублей. Ответчик возражений по "
    "существу требований не заявил. Суд считает иск подлежащим "
    "удовлетворению."
)

REVIEW = "ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ"


class TestReasoningRejected:
    def test_reasoning_is_not_usable(self) -> None:
        assert unusable_reason(REASONING) != ""

    def test_reason_named(self) -> None:
        """Причина должна называть, что не так, а не «текст плохой»."""
        reason = unusable_reason(REASONING)
        assert "русском" in reason or "рассужден" in reason

    def test_placeholder_replaces_text(self) -> None:
        text = not_formed_text(unusable_reason(REASONING), REVIEW)
        assert "Okay" not in text
        assert REVIEW in text
        assert "не сформирован" in text

    def test_english_in_quotes_still_rejected(self) -> None:
        """Русский текст с одной английской цитатой не должен проходить.

        Цитата из закона или из иностранного источника — обычное дело
        в судебном акте, и выбрасывать раздел из-за неё нельзя. Порог
        отсекает именно текст, который английский целиком.
        """
        mixed = (
            "Истец обратился в суд с требованием о взыскании 150 000 рублей "
            "по договору поставки. Ответчик возражений не заявил. Суд, "
            "руководствуясь статьей 310 Гражданского кодекса Российской "
            "Федерации, считает иск подлежащим удовлетворению в полном "
            "объеме. Решение вступает в силу по истечении срока "
            "апелляционного обжалования."
        )
        assert unusable_reason(mixed) == ""


class TestRepetitionRejected:
    def test_same_sentence_three_times(self) -> None:
        phrase = (
            "Суд считает исковые требования истца подлежащими "
            "удовлетворению в полном объеме. "
        )
        assert unusable_reason(phrase * 3) != ""

    def test_empty_answer(self) -> None:
        assert unusable_reason("   ") != ""

    def test_no_letters(self) -> None:
        assert unusable_reason("123 456 ---") != ""


class TestGoodTextAccepted:
    @pytest.mark.parametrize(
        "text",
        [
            RUSSIAN,
            "Руководствуясь ст. 194–199 ГПК РФ, суд решил: иск удовлетворить.",
            "Ответчик — ООО «Ромашка», ИНН 1234567890, заявил возражения.",
            "Сумма задолженности составила 15 000 руб. 00 коп. "
            "Взыскать с ответчика в пользу истца.",
        ],
    )
    def test_usable(self, text: str) -> None:
        assert unusable_reason(text) == ""


class TestRussianChatterRejected:
    """Живой прогон 22.10.2026: qvikhr в разделе «УСТАНОВИЛ».

    Модель начала не с текста раздела, а с описания своей работы:
    «Хорошо, давайте разберемся с этим делом. Начну с того, что в
    разделе "УСТАНОВИЛ" нужно описать выводы суда…». Английских
    маркеров в этой фразе нет ни одного, и раньше болтовня уходила в
    проект решения целиком.
    """

    CHATTER = (
        "Хорошо, давайте разберемся с этим делом. Начну с того, что "
        "в разделе \"УСТАНОВИЛ\" нужно описать выводы суда по "
        "требованиям, ссылаясь на нормы закона."
    )

    def test_chatter_is_not_usable(self) -> None:
        assert unusable_reason(self.CHATTER) != ""

    def test_reason_names_reasoning(self) -> None:
        reason = unusable_reason(self.CHATTER)
        assert "рассужден" in reason or "описала ход" in reason

    @pytest.mark.parametrize(
        "text",
        [
            # Живой прогон 31.10.2026 (vikhr-7b): вместо описательной
            # части пришёл ответ помощника о том, что материалы годятся.
            "здесь есть все необходимое для составления искового "
            "заявления по данному делу.",
            "Надеюсь, это поможет. Если вам нужно, я могу составить "
            "описательную часть подробнее.",
            "Вот текст раздела, который вы просили подготовить.",
        ],
    )
    def test_meta_answer_rejected(self, text: str) -> None:
        assert unusable_reason(text) != ""

    @pytest.mark.parametrize(
        "text",
        [
            # Формулировка суда с «удовлетворению» не должна ловиться.
            "Суд считает иск подлежащим удовлетворению частично.",
            # «Инструкция» в цитате — не признак болтовни.
            "Ответчик ссылался на инструкцию по эксплуатации "
            "и просил отказать в иске.",
            RUSSIAN,
        ],
    )
    def test_legal_text_is_not_chatter(self, text: str) -> None:
        assert unusable_reason(text) == ""


class TestMotivatingCompleteness:
    """Мотивировочная часть без обоснования — неполный проект."""

    def test_short_motivation_is_flagged(self) -> None:
        assert motivating_shortcoming("Суд считает иск подлежащим удовлетворению.")

    def test_empty_motivation_is_flagged(self) -> None:
        assert motivating_shortcoming("")

    def test_long_text_without_law_is_flagged(self) -> None:
        text = (
            "Суд рассмотрел обстоятельства дела и пришёл к выводу, что "
            "требования истца подтверждаются материалами. Ответчик не "
            "представил доказательств, опровергающих доводы истца. "
            "Договор был заключён, деньги переданы, возврат не произведён. "
        ) * 4
        assert "нет ссылок на нормы" in motivating_shortcoming(text)

    def test_reasoned_motivation_passes(self) -> None:
        text = (
            "Разрешая спор, суд руководствуется ст. 309, 310 ГК РФ: "
            "обязательства должны исполняться надлежащим образом. "
            "Довод ответчика о передаче денег без письменных доказательств "
            "отклоняется: в силу ст. 56, 60 ГПК РФ и ст. 162 ГК РФ "
            "несоблюдение простой письменной формы лишает стороны права "
            "ссылаться на свидетельские показания, а письменных "
            "доказательств не представлено. Поэтому требование о взыскании "
            "задолженности подлежит удовлетворению в полном размере. "
        ) * 2
        assert motivating_shortcoming(text) == ""
