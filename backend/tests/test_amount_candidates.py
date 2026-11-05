"""Детерминированное извлечение сумм: числа находит Python, не модель."""

from __future__ import annotations

from decimal import Decimal

from app.extraction.deterministic import classify_kind, extract_amounts

TEXT = """Цена иска: 1 256 850 (Один миллион двести пятьдесят шесть тысяч
восемьсот пятьдесят) рублей 00 копеек.
Государственная пошлина: 25 569 (Двадцать пять тысяч пятьсот шестьдесят
девять) рублей 00 копеек.
Во исполнение Договора Истец поставил товар общей стоимостью 2 450 000
(Два миллиона четыреста пятьдесят тысяч) рублей.
Ответчик произвёл предварительную оплату в размере 1 225 000 рублей.
Задолженность составляет 1 225 000 рублей 00 копеек.
Размер неустойки: 31 850 рублей.
Общая сумма исковых требований: 1 256 850 рублей.
"""


class TestExtractAmounts:
    def test_finds_all_distinct_sums(self) -> None:
        amounts = extract_amounts(TEXT, document_id="DOC-1")

        values = {str(item.value) for item in amounts}
        assert {"1256850", "25569", "2450000", "1225000", "31850"} <= values

    def test_kinds_are_classified(self) -> None:
        amounts = extract_amounts(TEXT, document_id="DOC-1")
        by_value = {str(item.value): item.kind for item in amounts}

        assert by_value["25569"] == "costs"
        assert by_value["31850"] == "penalty"
        # Одна и та же сумма в иске — и предоплата, и задолженность;
        # допустимы оба вида, важен не «долг» у оплаты.
        assert by_value["1225000"] in {"principal", "paid", "other"}
        assert by_value["1256850"] in {"claim", "total"}

    def test_debt_phrase_alone_is_principal(self) -> None:
        amounts = extract_amounts(
            "Задолженность составляет 1 225 000 рублей.", document_id="D"
        )

        assert amounts[0].kind == "principal"

    def test_duplicates_are_collapsed(self) -> None:
        amounts = extract_amounts(TEXT, document_id="DOC-1")

        principal = [item for item in amounts if item.value == Decimal("1225000")]
        assert len(principal) == 1

    def test_quote_is_present_and_narrow(self) -> None:
        amounts = extract_amounts(TEXT, document_id="DOC-1")

        for item in amounts:
            assert item.source is not None
            assert len(item.source.quote) <= 300
            assert "руб" in item.source.quote.lower()

    def test_year_without_roubles_is_not_amount(self) -> None:
        amounts = extract_amounts("Договор заключён в 2024 году.", document_id="D")

        assert amounts == []

    def test_empty_text(self) -> None:
        assert extract_amounts("") == []

    def test_kind_by_context(self) -> None:
        assert classify_kind("взыскать неустойку") == "penalty"
        assert classify_kind("государственная пошлина") == "costs"
        assert classify_kind("ничего не сказано") == "other"
