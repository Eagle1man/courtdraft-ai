"""Извлечение фактов по схеме: разбор значений, источники, повторы.

Проверяется то, что легко сломать незаметно: суммы и даты разбираются
Python-ом, а не моделью; непонятное значение становится пропуском, а не
правдоподобной подстановкой; факт без найденной цитаты не считается
подтверждённым.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.extraction.fact_extractor import (
    ExtractionRun,
    build_registry,
    extract_facts,
)
from app.extraction.schemas import (
    ExtractionPayload,
    ExtractionStatus,
    FactStatus,
    normalize_role,
    parse_date,
    parse_money,
)


@dataclass
class Chunk:
    text: str
    page: int | None = 1
    paragraph: int | None = 1


class FakeProvider:
    """Провайдер, который возвращает заранее заданные ответы по очереди."""

    name = "fake"

    def __init__(self, *answers: dict) -> None:
        self.answers = list(answers)
        self.requests: list = []

    async def generate_json(self, request, *, default=None):  # noqa: ANN001, ANN202
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        return dict(default or {})


CHUNKS = [
    Chunk(
        text=(
            "Истец ООО «Ромашка» просит взыскать с ответчика ООО «Лютик» "
            "задолженность по договору поставки от 15.01.2025 в размере "
            "1 000,00 руб. и пени 15,50 руб."
        ),
        page=2,
        paragraph=3,
    )
]

PAYLOAD = {
    "case_number": "2-123/2025",
    "participants": [
        {"role": "истец", "name": "ООО «Ромашка»", "quote": "Истец ООО «Ромашка»"},
        {"role": "ответчик", "name": "ООО «Лютик»", "quote": "ответчика ООО «Лютик»"},
    ],
    "amounts": [
        {"kind": "principal", "value": "1 000,00", "quote": "в размере 1 000,00 руб."}
    ],
    "dates": [
        {"kind": "contract", "value": "15.01.2025", "quote": "от 15.01.2025"}
    ],
    "claims": [
        {"subject": "взыскать задолженность", "amount": "1 000,00", "quote": "взыскать"}
    ],
    "facts": [
        {
            "text": "Договор поставки заключён 15.01.2025",
            "category": "contract",
            "quote": "по договору поставки от 15.01.2025",
        }
    ],
}


class TestValueParsing:
    def test_money_with_spaces_and_comma(self) -> None:
        assert parse_money("1 000 000,50") == Decimal("1000000.50")
        assert parse_money("2500.00 руб.") == Decimal("2500.00")

    def test_money_garbage_is_none(self) -> None:
        assert parse_money("сто тысяч") is None
        assert parse_money("") is None
        assert parse_money("-100") is None

    def test_money_with_roubles_words(self) -> None:
        assert parse_money("1 256 850 рублей") == Decimal("1256850")
        assert parse_money("2 450 000 (Два миллиона) рублей") == Decimal("2450000")
        assert parse_money("{1000.00} руб.") == Decimal("1000.00")

    def test_date_formats(self) -> None:
        assert parse_date("15.01.2025") == date(2025, 1, 15)
        assert parse_date("2025-01-15") == date(2025, 1, 15)
        assert parse_date("15 января 2025 г.") == date(2025, 1, 15)

    def test_impossible_date_is_none(self) -> None:
        assert parse_date("31.02.2025") is None

    def test_role_normalisation(self) -> None:
        assert normalize_role("Истец") == "plaintiff"
        assert normalize_role("ответчика") == "defendant"
        assert normalize_role("неизвестно") == "other"


class TestRegistryBuilding:
    def test_values_become_typed(self) -> None:
        registry = build_registry(
            ExtractionPayload.model_validate(PAYLOAD), CHUNKS, document_id="DOC-1"
        )

        assert registry.case_number == "2-123/2025"
        assert registry.amounts[0].value == Decimal("1000.00")
        assert registry.dates[0].value == date(2025, 1, 15)
        assert {item.role for item in registry.participants} == {
            "plaintiff",
            "defendant",
        }
        assert registry.status == ExtractionStatus.OK

    def test_quote_is_located_in_source(self) -> None:
        registry = build_registry(
            ExtractionPayload.model_validate(PAYLOAD), CHUNKS, document_id="DOC-1"
        )

        source = registry.amounts[0].source
        assert source is not None
        assert source.document_id == "DOC-1"
        assert source.page == 2
        assert source.paragraph == 3

    def test_unparsed_amount_is_missing_not_invented(self) -> None:
        payload = ExtractionPayload.model_validate(
            {"amounts": [{"kind": "principal", "value": "сто тысяч"}]}
        )

        registry = build_registry(payload, CHUNKS, document_id="DOC-1")

        assert registry.amounts == []
        assert registry.missing
        assert registry.missing[0].blocking is True
        assert registry.status == ExtractionStatus.NEEDS_REVIEW

    def test_fact_without_source_is_unsupported(self) -> None:
        payload = ExtractionPayload.model_validate(
            {"facts": [{"text": "Долг признан ответчиком", "quote": "нет такой фразы"}]}
        )

        registry = build_registry(payload, CHUNKS, document_id="DOC-1")

        assert registry.facts[0].status == FactStatus.UNSUPPORTED

    def test_fact_with_source_is_matched(self) -> None:
        registry = build_registry(
            ExtractionPayload.model_validate(PAYLOAD), CHUNKS, document_id="DOC-1"
        )

        assert registry.facts[0].status == FactStatus.SOURCE_MATCHED

    def test_empty_payload_needs_review(self) -> None:
        registry = build_registry(ExtractionPayload(), CHUNKS)

        assert registry.status == ExtractionStatus.NEEDS_REVIEW


class TestExtractionRun:
    async def test_valid_answer_returns_ok(self) -> None:
        provider = FakeProvider(PAYLOAD)

        run = await extract_facts(provider, CHUNKS, document_id="DOC-1")

        assert isinstance(run, ExtractionRun)
        assert run.status == ExtractionStatus.OK
        assert run.attempts == 1
        assert run.registry.amounts[0].value == Decimal("1000.00")

    async def test_schema_is_passed_to_model(self) -> None:
        provider = FakeProvider(PAYLOAD)

        await extract_facts(provider, CHUNKS)

        request = provider.requests[0]
        assert request.json_schema is not None
        assert "properties" in request.json_schema
        assert request.json_mode is True

    async def test_empty_answers_stop_after_limit(self) -> None:
        provider = FakeProvider({}, {})

        run = await extract_facts(provider, CHUNKS, attempts=2)

        assert run.attempts == 2
        assert run.status == ExtractionStatus.NEEDS_REVIEW
        assert len(run.errors) == 2
        assert run.registry.amounts == []

    async def test_broken_schema_is_retried_once(self) -> None:
        provider = FakeProvider({"amounts": "не список"}, PAYLOAD)

        run = await extract_facts(provider, CHUNKS, document_id="DOC-1")

        assert run.attempts == 2
        assert run.status == ExtractionStatus.OK
        assert run.registry.amounts

    async def test_no_text_does_not_call_model(self) -> None:
        provider = FakeProvider(PAYLOAD)

        run = await extract_facts(provider, [Chunk(text="   ")])

        assert run.attempts == 0
        assert run.status == ExtractionStatus.NEEDS_REVIEW
        assert provider.requests == []

    async def test_provider_error_is_needs_review(self) -> None:
        class Broken:
            name = "broken"

            async def generate_json(self, request, *, default=None):  # noqa: ANN001, ANN202
                raise RuntimeError("нет связи")

        run = await extract_facts(Broken(), CHUNKS, attempts=1)

        assert run.status == ExtractionStatus.NEEDS_REVIEW
        assert "нет связи" in run.errors[0]


@pytest.mark.parametrize("value", ["1 000,00", "1000.00", "1000"])
def test_money_variants_are_equal(value: str) -> None:
    assert parse_money(value) == Decimal("1000")
