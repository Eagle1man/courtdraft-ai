"""Тесты LLM-провайдеров.

Ключевая проверка п.69: MockLLMProvider и OllamaProvider удовлетворяют
одному протоколу LLMProvider. Если это перестанет быть так, переключение
MOCK -> REAL сломает агентов — тест ломается первым.
"""

from __future__ import annotations

import json

from app.config import LLMProviderName, Settings
from app.llm import build_llm_provider
from app.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMRole,
    LLMUsage,
)
from app.llm.mock import MockLLMProvider
from app.llm.ollama_provider import OllamaProvider


class TestProtocolCompliance:
    """Обе реализации — один интерфейс (п.5, п.69)."""

    def test_mock_satisfies_protocol(self) -> None:
        assert isinstance(MockLLMProvider(), LLMProvider)

    def test_ollama_satisfies_protocol(self) -> None:
        provider = OllamaProvider(base_url="http://localhost:11434", model="qwen3.5:4b")
        assert isinstance(provider, LLMProvider)

    def test_both_implement_same_methods(self) -> None:
        """Набор публичных методов должен совпадать."""
        mock = MockLLMProvider()
        ollama = OllamaProvider(base_url="http://localhost:11434", model="qwen3.5:4b")

        mock_methods = {m for m in dir(mock) if not m.startswith("_")}
        ollama_methods = {m for m in dir(ollama) if not m.startswith("_")}

        assert {"generate", "generate_json", "health", "name"} <= mock_methods
        assert mock_methods == ollama_methods


class TestMockProvider:
    """Mock детерминирован и предсказуем (п.39)."""

    async def test_generate_returns_response(self) -> None:
        provider = MockLLMProvider()
        response = await provider.generate(
            LLMRequest(messages=[LLMMessage(role="user", content="Привет")])
        )
        assert response.text
        assert response.is_mock is True
        assert response.provider == "mock"
        assert response.usage.total_tokens > 0

    async def test_deterministic(self) -> None:
        """Один и тот же вопрос — один и тот же ответ."""
        provider = MockLLMProvider()
        request = LLMRequest(messages=[LLMMessage(role="user", content="Одна и та же мысль")])

        first = await provider.generate(request)
        second = await provider.generate(request)

        assert first.text == second.text

    async def test_different_input_different_output(self) -> None:
        """Агент должен реагировать на вход, а не выдавать константу."""
        provider = MockLLMProvider()
        first = await provider.generate(
            LLMRequest(messages=[LLMMessage(role="user", content="Факт один")])
        )
        second = await provider.generate(
            LLMRequest(messages=[LLMMessage(role="user", content="Факт совсем другой")])
        )
        assert first.text != second.text

    async def test_marks_itself_as_mock(self) -> None:
        """Проверка обязана отличать заглушку от реального вывода модели."""
        provider = MockLLMProvider()
        response = await provider.generate(
            LLMRequest(messages=[LLMMessage(role="user", content="тест")])
        )
        assert response.is_mock is True
        assert "MOCK" in response.text

    async def test_json_for_participants(self) -> None:
        """Запрос про участников должен дать структуру документа (п.19)."""
        provider = MockLLMProvider()
        result = await provider.generate_json(
            LLMRequest(
                messages=[
                    LLMMessage(role="user", content="Извлеки участников и доказательства")
                ],
                json_mode=True,
            )
        )
        assert "participants" in result
        assert "claims" in result
        assert "evidence" in result
        assert "important_dates" in result

    async def test_json_for_verification(self) -> None:
        """Запрос проверки должен дать форму из п.29."""
        provider = MockLLMProvider()
        result = await provider.generate_json(
            LLMRequest(
                messages=[LLMMessage(role="user", content="Проверь проект решения")],
                json_mode=True,
            )
        )
        assert result.get("status") == "REVIEW_REQUIRED"
        assert "issues" in result

    async def test_generate_json_returns_dict(self) -> None:
        provider = MockLLMProvider()
        result = await provider.generate_json(
            LLMRequest(messages=[LLMMessage(role="user", content="Верни JSON")])
        )
        assert isinstance(result, dict)

    async def test_generate_json_fallback_on_default(self) -> None:
        """Нераспознанный запрос не должен ломать агента (п.23, п.27)."""
        provider = MockLLMProvider()
        default = {"laws": []}
        result = await provider.generate_json(
            LLMRequest(messages=[LLMMessage(role="user", content="абракадабра")]),
            default=default,
        )
        assert isinstance(result, dict)

    async def test_health_always_true(self) -> None:
        assert await MockLLMProvider().health() is True

    async def test_empty_message_set(self) -> None:
        """Пустой запрос не должен бросать исключение."""
        provider = MockLLMProvider()
        response = await provider.generate(LLMRequest(messages=[]))
        assert response.text


class TestRequestModel:
    """Модель запроса (п.3: модель не зашивается в код)."""

    def test_role_defaults(self) -> None:
        assert LLMRequest(messages=[]).role is LLMRole.REASONING

    def test_usage_total(self) -> None:
        assert LLMUsage(prompt_tokens=10, completion_tokens=5).total_tokens == 15

    def test_stop_sequence_preserved(self) -> None:
        request = LLMRequest(messages=[], stop=["КОНЕЦ"])
        assert request.stop == ["КОНЕЦ"]

    def test_metadata_does_not_break_serialisation(self) -> None:
        request = LLMRequest(messages=[], metadata={"node": "draft"})
        dumped = request.model_dump()
        assert dumped["metadata"]["node"] == "draft"

    def test_json_responses_are_parseable(self) -> None:
        """Модель-обёртка должна выдавать валидный JSON (нужно Ollama)."""
        provider = MockLLMProvider()
        text = provider._compose_json("Извлеки факты")
        parsed = json.loads(text)
        assert isinstance(parsed, dict)


class TestFactory:
    """Фабрика выбора провайдера (п.61)."""

    def test_mock_when_configured_mock(self) -> None:
        settings = Settings(llm_provider=LLMProviderName.MOCK)
        assert build_llm_provider(settings).name == "mock"

    def test_ollama_when_enabled(self) -> None:
        settings = Settings(
            llm_provider=LLMProviderName.OLLAMA,
            ollama_enabled=True,
            ollama_model="qwen3.5:4b",
        )
        assert build_llm_provider(settings).name == "ollama"

    def test_ollama_without_enabled_falls_back(self) -> None:
        """Противоречивая конфигурация не должна ронять систему (п.53)."""
        settings = Settings(llm_provider=LLMProviderName.OLLAMA, ollama_enabled=False)
        assert build_llm_provider(settings).name == "mock"

    def test_model_read_from_settings_not_hardcoded(self) -> None:
        """Смена модели = смена .env (п.3)."""
        settings = Settings(
            llm_provider=LLMProviderName.OLLAMA,
            ollama_enabled=True,
            ollama_model="другая:8b",
        )
        assert build_llm_provider(settings)._model == "другая:8b"  # noqa: SLF001


class TestCloudGuard:
    """Облако запрещено без явного ALLOW_CLOUD_LLM=true.

    Ключ и LLM_PROVIDER=openai сами по себе не разрешают отправку
    данных дела во внешний API.
    """

    def test_openai_without_allow_cloud_falls_back_to_mock(self) -> None:
        settings = Settings(
            llm_provider=LLMProviderName.OPENAI,
            openai_api_key="test-key",
            allow_cloud_llm=False,
        )
        assert build_llm_provider(settings).name == "mock"

    def test_openai_without_key_falls_back_to_mock(self) -> None:
        settings = Settings(
            llm_provider=LLMProviderName.OPENAI,
            openai_api_key="",
            allow_cloud_llm=True,
        )
        assert build_llm_provider(settings).name == "mock"

    def test_openai_with_explicit_permission_builds_provider(self) -> None:
        settings = Settings(
            llm_provider=LLMProviderName.OPENAI,
            openai_api_key="test-key",
            allow_cloud_llm=True,
        )
        assert build_llm_provider(settings).name == "openai"
