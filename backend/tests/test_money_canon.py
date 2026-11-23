"""Канон денег: копейки, составная форма, поля без валюты, таблицы."""
from __future__ import annotations

import pytest

KOPECK_LINE = (
    "Итого к взысканию процентов: 10 263,29 "
    "(Десять тысяч двести шестьдесят три) рубля 29 копеек."
)


def test_kopecks_are_not_rubles() -> None:
    from app.money import parse_money

    values = {t.kopecks for t in parse_money(KOPECK_LINE)}
    assert 1026329 in values
    assert 29 not in values
    assert 2900 not in values


def test_composite_form_words_match_digits() -> None:
    from app.money import parse_money

    tokens = parse_money("90 000 (Девяносто тысяч) рублей 00 копеек")
    assert any(t.kopecks == 9000000 for t in tokens)


def test_words_mismatch_is_flagged() -> None:
    from app.money import parse_money

    tokens = parse_money("90 000 (Девяносто тысяч) рублей 50 копеек")
    assert any(t.kopecks == 9000050 for t in tokens)
    bad = parse_money("90 000 (Девять тысяч) рублей 00 копеек")
    assert any(t.confidence < 1.0 for t in bad)


def test_bare_amount_in_typed_field() -> None:
    from app.money import parse_money

    text = "Основной долг: 90 000,00.\nГоспошлина: 2 900,00.\nВсего к взысканию: 103 163,29."
    values = {t.kopecks for t in parse_money(text)}
    assert {9000000, 290000, 10316329} <= values


def test_price_formula_field() -> None:
    from app.money import parse_money

    tokens = parse_money("Цена иска: 100 263,29 = 90 000,00 + 10 263,29 (ст. 91 ГПК РФ).")
    price = [t for t in tokens if t.unit_source == "typed_field"]
    assert price and price[0].kopecks == 10026329


@pytest.mark.parametrize(
    "text",
    [
        "договор от 10.02.2024",
        "ст. 395 ГК РФ",
        "РПО № 10500012345678",
        "ИНН 6161005770",
        "за 2024 год",
    ],
)
def test_non_money_numbers_excluded(text: str) -> None:
    from app.money import parse_money

    assert parse_money(text) == []


def test_format_roundtrip() -> None:
    from app.money import format_rub, parse_money

    for kopecks in (9000000, 1026329, 290000, 10316329, 10026329):
        rendered = format_rub(kopecks, style="words")
        assert any(t.kopecks == kopecks for t in parse_money(rendered)), rendered


def test_format_plain_has_two_kopecks() -> None:
    from app.money import format_rub

    assert format_rub(9000000, style="plain") == "90 000,00 руб."
    assert format_rub(1026329, style="plain") == "10 263,29 руб."


def test_no_float_and_g_in_claim_path() -> None:
    """В claim_render/claim_model не должно быть float-денег и %g."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "drafting"
    for name in ("claim_render.py", "claim_model.py"):
        path = root / name
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        assert ":g}" not in source, f"{name}: найден %g"
        assert "float(" not in source, f"{name}: найден float("
