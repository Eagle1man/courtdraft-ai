"""Сквозная проверка: выдуманная моделью сумма помечается в разделе.

Проверяется именно путь generate_draft → постобработка → текст раздела,
потому что раньше проверка утверждений срабатывала позже и сумма уже
стояла в акте как обычный текст.
"""

from __future__ import annotations

import asyncio

from app.agents.draft_agent import generate_draft
from app.agents.evidence_pack import EvidencePack
from app.llm.base import LLMResponse

TEMPLATE = {
    "name": "test",
    "sections": [
        {
            "key": "motivating",
            "name": "Мотивировочная часть",
            "purpose": "оценка доводов",
            "prompt_hint": "Оцени доводы.",
            "evidence_categories": ["legal_position"],
        }
    ],
}

INVENTED = (
    "Разрешая дело, суд исходит из следующего. Требования истца "
    "подлежат удовлетворению. Расходы на представителя составили "
    "6 470 руб. и подлежат взысканию с ответчика."
)


class _FixedLLM:
    """Провайдер, который всегда возвращает заданный текст."""

    name = "fixed"
    _model = "fixed"

    def __init__(self, text: str) -> None:
        self._text = text

    async def generate(self, request):  # noqa: ANN001, ANN201
        return LLMResponse(text=self._text, is_mock=False)


def _pack() -> EvidencePack:
    pack = EvidencePack(case_number="T-0003", amounts=["80 000", "4 000"])
    pack.documents["иск.docx"] = (
        "Истец просит взыскать долг 80 000 руб. и госпошлину 4 000 руб."
    )
    return pack


class TestInventedAmount:
    def test_invented_amount_is_marked_in_text(self) -> None:
        result = asyncio.run(
            generate_draft(_pack(), TEMPLATE, provider=_FixedLLM(INVENTED))
        )
        section = result.sections[0]
        assert "6 470 руб." in section.text
        assert "отсутствует в материалах" in section.text
        assert section.needs_review is True
        assert any("6 470" in gap for gap in section.gaps)

    def test_confirmed_amount_is_not_marked(self) -> None:
        text = (
            "Разрешая дело, суд исходит из следующего. Взыскать с "
            "ответчика долг 80 000 руб. и госпошлину 4 000 руб."
        )
        result = asyncio.run(
            generate_draft(_pack(), TEMPLATE, provider=_FixedLLM(text))
        )
        section = result.sections[0]
        assert "отсутствует в материалах" not in section.text
        assert not any("не найдена в материалах" in gap for gap in section.gaps)
