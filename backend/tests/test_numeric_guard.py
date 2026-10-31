"""Суммы в тексте сверяются с материалами дела (анти-галлюцинация).

Живой прогон: модель дописала «судебные расходы на представителя
6 470 рублей», которых нет ни в одном документе дела. Проверка должна
поймать такую сумму до того, как она уйдёт в проект акта.
"""

from app.drafting.numeric_guard import (
    check_unsourced_amounts,
    extract_money,
    mark_unsourced_amounts,
)

DOCUMENTS = [
    "Истец просит взыскать долг 80 000 руб., проценты 9 122,19 руб. "
    "и госпошлину 4 000 руб.",
]


class TestExtractMoney:
    def test_finds_amounts_with_currency(self) -> None:
        found = [value for _, value in extract_money("долг 80 000 рублей и 9 122,19 руб.")]
        assert found == [80000.0, 9122.19]

    def test_ignores_numbers_without_currency(self) -> None:
        assert extract_money("статья 333, пункт 4, дело 2-456/2025") == []


class TestUnsourcedAmounts:
    def test_confirmed_amount_is_not_flagged(self) -> None:
        text = "Взыскать долг 80 000 руб."
        assert check_unsourced_amounts(text, DOCUMENTS) == []

    def test_invented_amount_is_flagged(self) -> None:
        text = "Взыскать расходы на представителя 6 470 руб."
        found = check_unsourced_amounts(text, DOCUMENTS)
        assert [item.value for item in found] == [6470.0]

    def test_format_difference_is_not_a_hallucination(self) -> None:
        # В деле «9122,19», в тексте «9 122,19» — это одна и та же сумма.
        text = "Проценты 9 122,19 руб."
        assert check_unsourced_amounts(text, DOCUMENTS) == []

    def test_total_of_known_amounts_is_allowed(self) -> None:
        # 80 000 + 9 122,19 + 4 000 = 93 122,19 — итог, которого в деле
        # нет по определению, но он не выдуман.
        text = "Всего подлежит взысканию 93 122,19 руб."
        assert check_unsourced_amounts(text, DOCUMENTS) == []

    def test_total_with_repeated_addend_is_allowed(self) -> None:
        # На реальном акте: 27 500 × 3 = 82 500. Повтор слагаемого —
        # тоже арифметика, а не выдумка.
        text = "Всего взыскать 82 500 руб."
        assert check_unsourced_amounts(text, ["Ежемесячно 27 500 руб."]) == []

    def test_total_counted_inside_act_is_allowed(self) -> None:
        # Слагаемые есть только в самом акте, в материалах их нет.
        # Рядом со словом «всего» такой итог — арифметика.
        text = "Ежемесячно 27 500 руб. Всего: 82 500 руб."
        assert check_unsourced_amounts(text, ["Взыскать 27 500 руб."]) == []

    def test_sum_without_total_marker_is_not_excused(self) -> None:
        # Без привязки к «всего/итого» сумма не объясняется чужими числами.
        text = "Взыскать 82 000 руб."
        assert check_unsourced_amounts(text, ["27 500 руб."])

    def test_total_of_four_addends_is_allowed(self) -> None:
        text = "Всего 105 000 руб."
        sources = ["50 000 руб. долга, 10 000 руб. процентов, 10 000 руб. морального вреда, 35 000 руб. штрафа"]
        assert check_unsourced_amounts(text, sources) == []


class TestMarking:
    def test_marks_invented_amount_in_place(self) -> None:
        text = "Взыскать расходы 6 470 руб. с ответчика."
        marked, found = mark_unsourced_amounts(text, DOCUMENTS)
        assert found
        assert "6 470 руб." in marked
        assert "отсутствует в материалах" in marked

    def test_confirmed_text_is_unchanged(self) -> None:
        text = "Взыскать долг 80 000 руб."
        marked, found = mark_unsourced_amounts(text, DOCUMENTS)
        assert found == []
        assert marked == text
