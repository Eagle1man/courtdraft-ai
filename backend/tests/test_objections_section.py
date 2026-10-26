"""Раздел «Доводы ответчика» собирается правилами (п. 75 разбора)."""

import asyncio

from app.agents.draft_agent import generate_draft
from app.agents.evidence_pack import EvidenceItem, EvidencePack

TEMPLATE = {
    "name": "test",
    "sections": [
        {
            "key": "objections",
            "name": "Доводы ответчика",
            "purpose": "возражения дословно",
            "evidence_categories": ["objections"],
        }
    ],
}


def _pack(with_objections: bool) -> EvidencePack:
    pack = EvidencePack(case_number="T-0001")
    pack.items.append(
        EvidenceItem(
            category="claims",
            text="Истец просит взыскать задолженность 150 000 руб.",
            source_document="01_claim.txt",
            source_type="CLAIM",
            order=1,
        )
    )
    if with_objections:
        pack.items.append(
            EvidenceItem(
                category="objections",
                text="Ответчик просит отказать: услуги не оказывались.",
                source_document="02_response.txt",
                source_type="RESPONSE",
                order=2,
            )
        )
        pack.items.append(
            EvidenceItem(
                category="objections",
                text="Договор расторгнут по соглашению сторон.",
                source_document="03_letter.txt",
                source_type="LETTER",
                order=3,
            )
        )
    return pack


class _NoLLM:
    """Заглушка провайдера: правила модель не зовут, а если зовут — тест падает."""

    name = "no-llm"
    model = ""

    async def generate(self, request):  # noqa: ANN001, ANN201
        raise AssertionError("правила не должны обращаться к модели")


class TestObjectionsSection:
    def test_objections_quoted_verbatim(self) -> None:
        result = asyncio.run(
            generate_draft(_pack(True), TEMPLATE, provider=_NoLLM())
        )
        section = result.sections[0]
        assert section.key == "objections"
        assert section.is_mock is False
        assert section.needs_review is False
        assert "02_response.txt" in section.text
        assert "услуги не оказывались" in section.text
        assert "03_letter.txt" in section.text
        assert "расторгнут по соглашению сторон" in section.text

    def test_citations_list_sources(self) -> None:
        result = asyncio.run(
            generate_draft(_pack(True), TEMPLATE, provider=_NoLLM())
        )
        section = result.sections[0]
        assert section.citations == ["02_response.txt", "03_letter.txt"]

    def test_no_objections_flagged_for_review(self) -> None:
        result = asyncio.run(
            generate_draft(_pack(False), TEMPLATE, provider=_NoLLM())
        )
        section = result.sections[0]
        assert section.needs_review is True
        assert "не обнаружены" in section.text
        assert section.citations == []
        # Регрессия: fallback собран правилами, а не mock-провайдером —
        # без is_mock=False секция по умолчанию помечалась моком, и
        # движок проверки/интерфейс показывали честный текст как заглушку.
        assert section.is_mock is False
        assert section.built_by == "rules"

    def test_no_evaluation_in_text(self) -> None:
        # Оценка доводов — работа суда; в разделе её быть не должно.
        result = asyncio.run(
            generate_draft(_pack(True), TEMPLATE, provider=_NoLLM())
        )
        section = result.sections[0]
        for marker in ("не соответствует действительности", "отклоняются", "несостоятель"):
            assert marker not in section.text.lower()
