"""OpenAI-провайдер: текст, JSON, повторы, пустой reasoning, health.

Сеть не используется: httpx.MockTransport отдаёт заранее заданные
ответы. Ключ в тестах — фиктивный "test-key".
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm.base import LLMError, LLMMessage, LLMProvider, LLMRequest
from app.llm.openai_provider import OpenAIProvider

CHAT_OK = {
    "choices": [
        {
            "message": {"content": "Ответ модели"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


def make_provider(
    handler: httpx.MockTransport,
    **kwargs: object,
) -> OpenAIProvider:
    return OpenAIProvider(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        model="test-model",
        model_auto=False,
        transport=handler,
        **kwargs,
    )


async def test_protocol_compliance() -> None:
    provider = make_provider(httpx.MockTransport(lambda request: httpx.Response(200)))
    assert isinstance(provider, LLMProvider)


async def test_generate_returns_text_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json=CHAT_OK)

    provider = make_provider(httpx.MockTransport(handler))
    response = await provider.generate(
        LLMRequest(messages=[LLMMessage(role="user", content="Привет")])
    )
    assert response.text == "Ответ модели"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5
    assert response.model == "test-model"
    assert response.is_mock is False


async def test_generate_json_parses_fenced_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "choices": [
                {
                    "message": {"content": "```json\n{\"ok\": true}\n```"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return httpx.Response(200, json=payload)

    provider = make_provider(httpx.MockTransport(handler))
    result = await provider.generate_json(
        LLMRequest(messages=[LLMMessage(role="user", content="Верни JSON")])
    )
    assert result == {"ok": True}


async def test_retries_on_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json=CHAT_OK)

    provider = make_provider(
        httpx.MockTransport(handler),
        retries=1,
        retry_delay=0.0,
    )
    response = await provider.generate(
        LLMRequest(messages=[LLMMessage(role="user", content="Привет")])
    )
    assert response.text == "Ответ модели"
    assert calls["n"] == 2


async def test_401_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    provider = make_provider(
        httpx.MockTransport(handler),
        retries=3,
        retry_delay=0.0,
    )
    with pytest.raises(LLMError):
        await provider.generate(
            LLMRequest(messages=[LLMMessage(role="user", content="Привет")])
        )
    assert calls["n"] == 1


async def test_empty_content_retries_with_more_tokens() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content.decode("utf-8"))
        if calls["n"] == 1:
            assert body["max_tokens"] == 700
            payload = {
                "choices": [
                    {
                        "message": {"content": "", "reasoning": "долго думал"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 700},
            }
            return httpx.Response(200, json=payload)
        assert body["max_tokens"] == 1400
        assert body["reasoning_effort"] == "none"
        return httpx.Response(200, json=CHAT_OK)

    provider = make_provider(httpx.MockTransport(handler))
    response = await provider.generate(
        LLMRequest(messages=[LLMMessage(role="user", content="Привет")])
    )
    assert response.text == "Ответ модели"
    assert calls["n"] == 2


async def test_health_ok_for_known_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={"data": [{"id": "test-model"}]},
        )

    provider = make_provider(httpx.MockTransport(handler))
    assert await provider.health() is True


async def test_health_false_without_key() -> None:
    provider = OpenAIProvider(
        base_url="https://example.invalid/v1",
        api_key="",
        model="test-model",
        model_auto=False,
    )
    assert await provider.health() is False
