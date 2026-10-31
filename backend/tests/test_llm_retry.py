"""Сбой модели при генерации раздела: вторая попытка, а не пустой акт.

Ollama на 7,8 ГБ может уронить процесс модели посреди ответа —
проверено: раздел «Мотивировочная» остался пустым из-за 500
«connection forcibly closed». Это технический сбой, а не отказ модели
писать, и у него уже есть вторая попытка с якорным промптом.
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
            "purpose": "оценка доказательств",
            "prompt_hint": "Изложи мотивы.",
            "evidence_categories": ["evidence"],
        }
    ],
}


class _FlakyLLM:
    """Первый вызов рвётся соединением, второй отвечает нормально."""

    name = "flaky"
    _model = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError(
                "Ollama вернула 500: an error was encountered while running "
                "the model: wsarecv: connection forcibly closed"
            )
        return LLMResponse(
            text=(
                "Суд пришёл к выводу, что ответчик нарушил срок оплаты по "
                "договору поставки, и требование истца подлежит "
                "удовлетворению в заявленном размере."
            ),
            model="flaky",
            provider="flaky",
            is_mock=False,
            latency_ms=1000,
        )


class TestTransientModelFailure:
    def test_second_attempt_after_model_crash(self) -> None:
        provider = _FlakyLLM()
        result = asyncio.run(
            generate_draft(EvidencePack(case_number="2-1/2026"), TEMPLATE, provider=provider)
        )
        section = result.sections[0]
        assert provider.calls == 2, "после сбоя модель должна быть вызвана ещё раз"
        assert section.text.strip(), "раздел не должен оставаться пустым"
        assert "ответчик нарушил срок оплаты" in section.text
        assert section.error == ""
