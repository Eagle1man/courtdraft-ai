"""«УСТАНОВИЛ» собирается по материалам, если модель вернула заголовок.

Обе Vikhr-модели на этом промпте отвечают фразой-заголовком и
останавливаются (проверено на уровне API Ollama). Пустой раздел в акте —
дыра, поэтому есть фактическая замена: обстоятельства из материалов с
указанием документа, без оценок и выводов.
"""

from __future__ import annotations

import asyncio

from app.agents.draft_agent import generate_draft
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.llm.base import LLMResponse

TEMPLATE = {
    "name": "test",
    "sections": [
        {
            "key": "established",
            "name": "УСТАНОВИЛ",
            "purpose": "обстоятельства",
            "evidence_categories": ["claims", "evidence"],
        }
    ],
}


def _pack() -> EvidencePack:
    pack = EvidencePack(case_number="2-1/2026", court_name="Суд")
    pack.documents["01_claim.txt"] = "ИСКОВОЕ ЗАЯВЛЕНИЕ о взыскании 150 000 руб."
    pack.items.append(
        EvidenceItem(
            category="claims",
            text="Взыскать с ответчика задолженность 150 000 руб. по договору поставки.",
            source_document="01_claim.txt",
            source_type="CLAIM",
            order=1,
        )
    )
    pack.items.append(
        EvidenceItem(
            category="evidence",
            text="Договор поставки заключён 10 января 2025 года.",
            source_document="02_contract.txt",
            source_type="CONTRACT",
            order=2,
        )
    )
    return pack


class _HeadingOnlyLLM:
    """Модель, которая отвечает заголовком и останавливается."""

    name = "stub"
    _model = "stub"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):  # noqa: ANN001, ANN201
        self.calls += 1
        return LLMResponse(
            text="суд установил следующее:",
            model="stub",
            provider="stub",
            is_mock=False,
            latency_ms=100,
        )


class TestEstablishedFallback:
    def test_heading_only_answer_is_replaced_with_facts(self) -> None:
        provider = _HeadingOnlyLLM()
        result = asyncio.run(generate_draft(_pack(), TEMPLATE, provider=provider))
        section = result.sections[0]

        assert provider.calls >= 1, "модель должна была попробовать"
        assert section.text.startswith("СУД УСТАНОВИЛ:")
        assert "150 000" in section.text
        assert "заключён 10 января 2025" in section.text
        assert section.built_by == "rules"
        assert section.is_mock is False
        assert section.needs_review is True
        assert section.citations == ["01_claim.txt", "02_contract.txt"]
        assert any("собран по материалам" in gap for gap in section.gaps)

    def test_fallback_quotes_only_materials(self) -> None:
        """Ни одного числа, которого нет в материалах."""
        result = asyncio.run(
            generate_draft(_pack(), TEMPLATE, provider=_HeadingOnlyLLM())
        )
        text = result.sections[0].text
        for value in ("150 000", "10 января 2025"):
            assert value in text
        assert "123 456" not in text

    def test_empty_pack_is_marked_not_invented(self) -> None:
        pack = EvidencePack(case_number="2-2/2026", court_name="Суд")
        result = asyncio.run(
            generate_draft(pack, TEMPLATE, provider=_HeadingOnlyLLM())
        )
        section = result.sections[0]
        assert "не содержат обстоятельств" in section.text
        assert section.built_by == "rules"
        assert section.needs_review is True
