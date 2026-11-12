"""Тесты проверки персон и ролей (Фаза 10).

Проверка должна ловить выдумки модели и НЕ мешать нормальному тексту.
Вторая половина тестов важнее первой: ложное срабатывание в судебном
проекте хуже пропуска, потому что на него будут тратить время.
"""

from __future__ import annotations

from app.agents.evidence_pack import (
    EvidenceItem,
    EvidencePack,
)
from app.verification.persons import (
    check_composition,
    check_persons,
    check_roles,
    known_persons,
)


def make_pack() -> EvidencePack:
    """Пакет по демо-делу: истец, ответчик, договор, сумма."""
    pack = EvidencePack(
        case_number="DEMO-001",
        court_name="Демонстрационный районный суд",
        category="договорные споры",
        participants=[
            "Образцов Пётр Петрович (истец)",
            "Фирма «Вымышленная Строй» (ответчик)",
        ],
        amounts=["150000"],
        dates=["15 мая 2026"],
    )
    pack.items = [
        EvidenceItem(
            category="claims",
            text=(
                "Исковое заявление подано Образцовым Петром Петровичем "
                "к Фирме «Вымышленная Строй» о взыскании 150000 рублей."
            ),
            source_document="01_claim.txt",
            source_type="CLAIM",
            amounts=["150000"],
        ),
        EvidenceItem(
            category="evidence",
            text=(
                "Договор поставки № Д-2026/001 от 15 мая 2026 года. "
                "Покупатель: Образцов Пётр Петрович. "
                "Ответчик: Фирма «Вымышленная Строй»."
            ),
            source_document="03_contract.txt",
            source_type="CONTRACT",
        ),
    ]
    return pack


class TestKnownPersons:
    def test_participants_discovered(self) -> None:
        names = known_persons(make_pack())
        assert "образцов пётр петрович" in names
        assert "фирма «вымышленная строй»" in names

    def test_surnames_added(self) -> None:
        """Совпадение по фамилии должно засчитываться.

        В материалах может быть «Иванов Иван», а модель написала
        «Иванов Иван Иванович» — это тот же человек, а не выдумка.
        """
        names = known_persons(make_pack())
        assert "образцов" in names


class TestInventedPersons:
    def test_invented_judge_found(self) -> None:
        """Находка из реального прогона: судья, которого нет в деле."""
        findings = check_persons(
            "Судья: Иванов Иван Иванович",
            "Вводная часть",
            make_pack(),
        )
        assert len(findings) == 1
        assert "Иванов Иван Иванович" in findings[0].person

    def test_known_participant_passes(self) -> None:
        findings = check_persons(
            "Истец Образцов Пётр Петрович обратился в суд.",
            "УСТАНОВИЛ",
            make_pack(),
        )
        assert findings == []

    def test_role_word_is_not_a_person(self) -> None:
        """«Истец Образцов Пётр» — это роль и фамилия, а не ФИО.

        Без этой проверки каждая фраза с обращением к стороне
        считалась бы выдуманной персоной.
        """
        findings = check_persons(
            "Ответчик Фирма «Вымышленная Строй» не исполнил обязательство.",
            "УСТАНОВИЛ",
            make_pack(),
        )
        assert findings == []

    def test_invented_with_initials_found(self) -> None:
        findings = check_persons(
            "Установил: судья Петров П. П.",
            "Вводная часть",
            make_pack(),
        )
        assert len(findings) == 1
        assert "Петров" in findings[0].person

    def test_repeated_word_is_not_fio(self) -> None:
        """Три одинаковых слова — не ФИО."""
        findings = check_persons(
            "Образцов Образцов Образцов",
            "Описательная часть",
            make_pack(),
        )
        assert findings == []

    def test_same_person_reported_once(self) -> None:
        text = "Иванов Иван Иванович. Иванов Иван Иванович. Иванов Иван Иванович."
        findings = check_persons(text, "Вводная часть", make_pack())
        assert len(findings) == 1


class TestCourtComposition:
    def test_prosecutor_in_composition_flagged(self) -> None:
        """Прокурор не входит в состав суда — верный признак сочинённого текста."""
        findings = check_composition(
            "Состав суда: прокурор, судья, секретарь",
            "Вводная часть",
            make_pack(),
        )
        assert findings
        assert any("прокурор" in f.reason for f in findings)

    def test_unknown_judge_in_composition_flagged(self) -> None:
        findings = check_composition(
            "Судьи: Иванов И. И., Петрова А. А.",
            "Вводная часть",
            make_pack(),
        )
        assert len(findings) == 2

    def test_plain_mention_of_judge_passes(self) -> None:
        """Обычная фраза «судья рассмотрел дело» — не состав суда."""
        findings = check_composition(
            "Судья рассмотрел дело и вынес решение.",
            "Описательная часть",
            make_pack(),
        )
        assert findings == []

    def test_composition_without_persons_passes(self) -> None:
        findings = check_composition(
            "Состав суда: единоличный судья",
            "Вводная часть",
            make_pack(),
        )
        assert findings == []


class TestRoleSwapping:
    """Самая опасная ошибка: сторона, которая должна получить деньги,
    назначена плательщиком."""

    def test_plaintiff_made_payer(self) -> None:
        """Находка из реального прогона модели."""
        findings = check_roles(
            "Образцов Пётр Петрович обязуется в течение месяца "
            "со дня вручения решения оплатить сумму 150000 рублей.",
            "РЕШИЛ",
            make_pack(),
        )
        assert findings, "плательщик-истец должен быть найден"

    def test_collection_from_plaintiff(self) -> None:
        findings = check_roles(
            "Суд взыскивает с истца задолженность в размере 30000 рублей.",
            "РЕШИЛ",
            make_pack(),
        )
        assert any("взыскание" in f.detail for f in findings)

    def test_correct_collection_passes(self) -> None:
        """Нормальная формулировка предписания замечанием не считается."""
        findings = check_roles(
            "Суд взыскивает с ответчика в пользу истца 150000 рублей.",
            "РЕШИЛ",
            make_pack(),
        )
        assert findings == []

    def test_defendant_paying_is_normal(self) -> None:
        """Ответчик платит — это и есть взыскание, а не ошибка."""
        findings = check_roles(
            "Ответчик обязан уплатить истцу 150000 рублей.",
            "РЕШИЛ",
            make_pack(),
        )
        assert findings == []

    def test_debt_ownership_not_a_role_error(self) -> None:
        """«с истца задолженность» — принадлежность, а не платёж.

        Связка «должен» здесь означала бы ложное срабатывание.
        """
        findings = check_roles(
            "Суд взыскивает с истца задолженность в размере 30000 рублей.",
            "Описательная часть",
            make_pack(),
        )
        assert not any("плательщиком" in f.detail for f in findings)

    def test_law_mention_skipped(self) -> None:
        """В цитате нормы стороны перечисляются, роли там не разбираются."""
        findings = check_roles(
            "Согласно статье 51 ГПК РФ стороны вправе знакомиться "
            "с материалами дела, истец и ответчик в равной мере.",
            "Мотивировочная часть",
            make_pack(),
        )
        assert findings == []

    def test_case_forms_recognised(self) -> None:
        """Падежные формы ролей должны распознаваться."""
        for phrase in (
            "Истцу надлежит уплатить 150000 рублей.",
            "Истец должен перечислить 150000 рублей.",
        ):
            findings = check_roles(phrase, "РЕШИЛ", make_pack())
            assert findings, f"не распознано: {phrase}"

    def test_claimant_collecting_is_not_payer(self) -> None:
        """«Истцом взыскано/удержано» — действие взыскателя, не платёж."""
        for phrase in (
            "Истцом взыскано 150000 рублей.",
            "Истцом удержано из пенсии 802 рубля.",
        ):
            findings = check_roles(phrase, "РЕШИЛ", make_pack())
            assert not any("плательщиком" in f.detail for f in findings)

    def test_plaintiff_states_defendant_does_not_pay(self) -> None:
        """Истец сообщает о неплатеже ответчика — плательщик ответчик."""
        phrase = (
            "Истец указал, что ответчик нарушает свои обязательства "
            "по договору и не вносит абонентскую плату."
        )
        findings = check_roles(phrase, "Мотивировочная часть", make_pack())
        assert not any("плательщиком" in f.detail for f in findings)

    def test_duplicates_collapsed(self) -> None:
        """Одна и та же находка не должна повторяться."""
        findings = check_roles(
            "Истец обязуется уплатить 150000 рублей. "
            "Истец обязуется уплатить 150000 рублей.",
            "РЕШИЛ",
            make_pack(),
        )
        assert len(findings) == 1

    def test_one_finding_per_sentence(self) -> None:
        """Полное ФИО истца в одном предложении даёт одну находку.

        Иначе «Образцов Пётр Петрович обязуется уплатить» породил бы
        три одинаковых замечания — по фамилии, имени и полному имени.
        """
        findings = check_roles(
            "Образцов Пётр Петрович обязуется уплатить 150000 рублей.",
            "РЕШИЛ",
            make_pack(),
        )
        assert len(findings) == 1
        assert "плательщиком" in findings[0].detail


class TestNoFalsePositivesOnLiveOutput:
    """Проверки на тексте из настоящего прогона модели.

    Здесь собраны фразы, которые правило обязано пропустить. Каждая из
    них когда-то давала ложное CRITICAL: дательный падеж «истцу»
    принимался за плательщика, хотя платил ответчик.
    """

    def test_dative_is_recipient_not_payer(self) -> None:
        phrase = (
            "В связи с этим, суд установил, что ответчик обязан возместить "
            "истцу сумму 150000 рублей, а также уплатить ущерб в размере "
            "5100 рублей."
        )
        assert check_roles(phrase, "РЕШИЛ", make_pack()) == []

    def test_claim_of_plaintiff_is_not_payment(self) -> None:
        """«Требует возмещения» — это содержание иска, а не платёж."""
        phrase = (
            "Образцов Пётр Петрович, являясь собственником имущества, "
            "требует от Фирмы «Вымышленная Строй» возмещения денежных "
            "средств в размере 150000 рублей."
        )
        assert check_roles(phrase, "РЕШИЛ", make_pack()) == []

    def test_payer_after_recipient(self) -> None:
        phrase = "Ответчик перечислит истцу 150000 рублей."
        assert check_roles(phrase, "РЕШИЛ", make_pack()) == []

    def test_dative_subject_is_payer(self) -> None:
        """Но «Истцу надлежит уплатить» — это всё-таки плательщик."""
        findings = check_roles("Истцу надлежит уплатить 150000 рублей.", "РЕШИЛ", make_pack())
        assert findings


class TestPackWithoutParticipants:
    def test_everything_flagged_when_pack_empty(self) -> None:
        """Пустой пакет — нет ни одной известной персоны.

        Проверка обязана сообщить об этом, а не молчать: отсутствие
        участников в пакете — это пробел в материалах, а не чистота.
        """
        empty = EvidencePack(case_number="X", court_name="Y", category="Z")
        findings = check_persons("Судья: Иванов Иван Иванович", "Вводная часть", empty)
        assert findings
