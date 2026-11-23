"""Регрессия P0-01: итог расчёта не должен брать число копеек."""
from __future__ import annotations

CALC_TAIL = (
    "Итого к взысканию процентов: 10 263,29 "
    "(Десять тысяч двести шестьдесят три) рубля 29 копеек."
)


def test_total_is_not_29() -> None:
    from app.drafting.totals import total_from_texts

    found = total_from_texts([CALC_TAIL])
    assert found is not None
    assert abs(found.amount - 10263.29) < 0.005, found.amount


def test_total_priority_total_over_itogo() -> None:
    from app.drafting.totals import total_from_texts

    text = (
        "Итого к взысканию процентов: 10 263,29 руб.\n"
        "Всего к взысканию: 103 163,29 руб.\n"
    )
    found = total_from_texts([text])
    assert found is not None
    assert abs(found.amount - 103163.29) < 0.005, found.amount


def test_table_itogo_row_not_days() -> None:
    """Строка '| |ИТОГО||207||||10 263,29|' — итог сумма, а не 207 дней."""
    from app.drafting.totals import total_from_texts

    text = "| |ИТОГО||207||||10 263,29|10 263,29|\n"
    found = total_from_texts([text])
    assert found is not None
    assert found.amount > 1000, found.amount
