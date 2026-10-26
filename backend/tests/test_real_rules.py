"""Тесты правил, выведенных из настоящих судебных решений.

Источник правил: разбор 1 425 гражданских дел с резолютивной частью
из датасета TryDotAtwo/russian-legal-ner. Числа и выводы — в
docs/real-resolutions-rules.md, разбор воспроизводится скриптом
scripts/extract_rules.py.

Каждый тест здесь отвечает на один вопрос, который раньше не был
задан: так ли суд пишет резолютивную часть на самом деле.
"""

from __future__ import annotations

import pytest

from app.agents.evidence_pack import EvidencePack
from app.drafting.operative import (
    MoneyClaim,
    Outcome,
    Parties,
    build_operative,
    rubles_to_words,
)
from app.drafting.requirements import extract_parties


def gas_claim() -> MoneyClaim:
    """Требование из реального решения (Газпром межрегионгаз, 2013)."""
    return MoneyClaim(
        amount=5034,
        kopecks=79,
        basis="задолженность по оплате за предоставленный природный газ",
        source="начисление за период с 01.08.2012 по 30.11.2012",
    )


class TestMoneyFormat:
    """Копейки пишутся в решениях почти так же часто, как рубли."""

    def test_kopecks_shown(self) -> None:
        text = build_operative([gas_claim()], fee_paid=400).render()
        assert "5 034 руб. 79 коп." in text

    def test_no_kopecks_when_zero(self) -> None:
        claim = MoneyClaim(amount=150000, basis="долг", source="договор")
        text = build_operative([claim], fee_paid=5100).render()
        assert "150 000 руб. 00 коп." in text

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, "один"),
            (2, "два"),
            (11, "одиннадцать"),
            (21, "двадцать один"),
            (79, "семьдесят девять"),
            (100, "сто"),
            (5467, "пять тысяч четыреста шестьдесят семь"),
            (3034, "три тысячи тридцать четыре"),
            (12000, "двенадцать тысяч"),
            (1000, "одна тысяча"),
        ],
    )
    def test_rubles_to_words(self, value: int, expected: str) -> None:
        assert rubles_to_words(value) == expected

    def test_words_in_brackets(self) -> None:
        """Сумма прописью стоит в скобках после цифр.

        Итог по делу ООО «Газпром межрегионгаз»: 5034 руб. 79 коп.
        плюс пошлина 400 руб. — это 5434 руб. 79 коп.
        """
        result = build_operative([gas_claim()], fee_paid=400)
        text = result.render()
        assert "всего подлежит взысканию" in text.lower()
        assert "(пять тысяч четыреста тридцать четыре)" in text


class TestTotalLine:
    """Итог «а всего» — одна цифра на всё взыскание."""

    def test_total_includes_fee(self) -> None:
        """5034,79 + 400 = 5434,79 — итог включает пошлину.

        Копейки не теряются: складываются отдельно от рублей, иначе
        итог вышел бы «5 434 руб. 00 коп.» вместо «79 коп.».
        """
        result = build_operative([gas_claim()], fee_paid=400)
        assert "5 434" in result.render()
        assert "79 коп." in result.render()

    def test_total_absent_when_nothing_granted(self) -> None:
        """При полном отказе итога быть не должно."""
        claim = MoneyClaim(amount=150000, basis="убытки",
                           source="экспертиза", outcome=Outcome.DENIED)
        result = build_operative([claim], fee_paid=None)
        assert "Всего подлежит взысканию" not in result.render()

    def test_total_can_be_switched_off(self) -> None:
        result = build_operative([gas_claim()], fee_paid=400, total_line=False)
        assert "Всего подлежит взысканию" not in result.render()

    def test_total_uses_granted_not_claimed_sum(self) -> None:
        """При частичном удовлетворении итог меньше заявленного.

        Иначе в проекте окажется сумма, которую суд не взыскал, и
        исполнительный лист будет выдан на неё.

        Пошлина при частичном удовлетворении в итог НЕ входит: ч. 1
        ст. 98 ГПК РФ присуждает её пропорционально удовлетворённой
        части, а пропорцию считает судья. Раньше итог был «90 000 +
        5 100 пошлина», проект противоречил сам себе — пункт о
        пошлинной пропорции стоял строкой выше.
        """
        claim = MoneyClaim(amount=150000, granted_amount=90000,
                           basis="неустойка", source="договор",
                           outcome=Outcome.PARTIAL)
        result = build_operative([claim], fee_paid=5100)
        text = result.render()
        assert "90 000" in text
        assert "155 100" not in text
        assert "95 100" not in text
        # Пункт о пошлине перешёл на пропорциональную формулировку.
        assert "пропорционально" in text
        assert "ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ" in text

    def test_fee_full_amount_when_outcome_unknown(self) -> None:
        """Если исход не определён (требование без частичности) — пошлина полной суммой.

        Пропорциональность применяется только когда есть что делить:
        частичное удовлетворение или отказ в части. Требование истца
        без сигналов из материалов остаётся в полном объёме, и пошлина
        взыскивается полностью.
        """
        claim = MoneyClaim(amount=150000, basis="убытки",
                           source="договор", outcome=Outcome.SATISFIED)
        result = build_operative([claim], fee_paid=5100)
        text = result.render()
        assert "155 100" in text
        assert "пропорционально" not in text


class TestNamedParties:
    """Суд называет стороны конкретно чаще, чем родовыми словами."""

    def test_named_parties_used(self) -> None:
        """Конструкция с ФИО сторон, а не «с ответчика»."""
        parties = Parties(plaintiff="ООО «Газпром межрегионгаз Волгоград»",
                          defendant="Абдулиной М.И.")
        result = build_operative([gas_claim()], fee_paid=400, parties=parties)
        text = result.render()
        assert "с Абдулиной М.И. в пользу ООО «Газпром" in text

    def test_falls_back_to_roles_when_unknown(self) -> None:
        """Пустое имя заменяется родовым словом, а не оставляет дыру."""
        result = build_operative([gas_claim()], fee_paid=400)
        assert "взыскать с ответчика в пользу истца" in result.render()

    def test_partial_name_is_still_used(self) -> None:
        """Известен только ответчик — подставляется он."""
        parties = Parties(plaintiff="", defendant="Абдулиной М.И.")
        result = build_operative([gas_claim()], fee_paid=400, parties=parties)
        assert "с Абдулиной М.И. в пользу истца" in result.render()


class TestExtractParties:
    def test_participants_split_by_role(self) -> None:
        pack = EvidencePack(participants=[
            "Образцов Пётр Петрович (истец)",
            'Фирма «Вымышленная Строй» (ответчик)',
        ])
        parties = extract_parties(pack)
        assert parties.plaintiff == "Образцов Пётр Петрович"
        assert parties.defendant == 'Фирма «Вымышленная Строй»'

    def test_missing_participant_gives_empty(self) -> None:
        """Нет участника — пусто, а не выдуманное имя."""
        parties = extract_parties(EvidencePack())
        assert parties.plaintiff == ""
        assert parties.defendant == ""


class TestKopecksFromMaterials:
    def test_kopecks_extracted(self) -> None:
        """Копейки читаются из материалов, а не прибавляются нулями."""
        from app.drafting.requirements import _find_amount_with_kopecks

        assert _find_amount_with_kopecks("в размере 5034 руб. 79 коп.") == (
            5034, 79,
        )

    def test_kopecks_without_money_word_ignored(self) -> None:
        from app.drafting.requirements import _find_amount_with_kopecks

        found = _find_amount_with_kopecks("дело № 5-79/2019 рассмотрено")
        assert found is None


class TestRealDecisionShape:
    """Сверка с формой, в которой решение пишет суд."""

    def test_matches_structure_of_real_decision(self) -> None:
        """Резолюция заканчивается правом на обжалование."""
        result = build_operative([gas_claim()], fee_paid=400)
        assert "обжаловано" in result.items[-1]

    def test_starts_with_legal_basis(self) -> None:
        """Основание — ст. 194–199 ГПК РФ, а не ст. 209-211.

        Прежние ст. 209-211 стояли в шаблоне без проверки, и разбор
        настоящих решений это опроверг: в 1 425 гражданских
        резолюциях они не встречаются ни разу. Основание по существу —
        ст. 198 ГПК, но формула «Руководствуясь ст. 198 ГПК РФ»
        неполная: суд принимает решение по ст. 194–199 ГПК РФ.
        """
        text = build_operative([gas_claim()], fee_paid=400).render()
        assert text.startswith("Руководствуясь ст. 194–199 ГПК РФ, суд решил:")

    def test_legal_basis_is_overridable(self) -> None:
        """Для заочного производства основание задаётся своё."""
        result = build_operative(
            [gas_claim()], fee_paid=400, legal_basis="ст. 194–199, 233 ГПК РФ"
        )
        assert result.render().startswith(
            "Руководствуясь ст. 194–199, 233 ГПК РФ, суд решил:"
        )

    def test_old_articles_are_gone(self) -> None:
        """Статьи 209-211 больше не попадают в проект решения."""
        text = build_operative([gas_claim()], fee_paid=400).render()
        header = text.split("\n1. ")[0]
        for article in ("209", "210", "211"):
            assert article not in header

    def test_items_numbered(self) -> None:
        text = build_operative([gas_claim()], fee_paid=400).render()
        assert "\n1. " in text and "\n2. " in text


class TestSelfcheckAfterUpdate:
    def test_selfcheck_clean(self) -> None:
        from app.drafting.operative import selfcheck

        assert selfcheck() == []
