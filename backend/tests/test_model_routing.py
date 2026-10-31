"""Маршрутизация и кэш ответов модели (Этап 7).

Проверяется два обещания этапа:

1. маршрут по умолчанию не меняет модель — «качество не ниже» начинается
   с того, что без явной настройки ничего не подменяется;
2. кэш отдаёт тот же ответ, а не похожий, и не притворяется попаданием,
   когда запрос изменился или срок хранения истёк.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.base import LLMRequest, LLMResponse, LLMRole
from app.model_routing.cache import ResponseCache, get_response_cache, reset_response_cache
from app.model_routing.registry import TaskKind, resolve_route, routes_snapshot


def _request(prompt: str = "текст", **kwargs) -> LLMRequest:  # noqa: ANN003
    options = {"max_tokens": 420, **kwargs}
    return LLMRequest(
        messages=[{"role": "user", "content": prompt}],
        role=LLMRole.GENERATION,
        temperature=0.1,
        **options,
    )


class _CountingProvider:
    """Провайдер-счётчик: без него «кэш работает» неотличимо от «повезло»."""

    name = "counting"
    _model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            text=f'ответ-{self.calls}: {request.messages[-1].content}',
            model=self._model,
            provider=self.name,
            is_mock=True,
            latency_ms=1000,
        )


class TestRoutes:
    def test_default_route_is_configured_model(self) -> None:
        settings = Settings(_env_file=None, ollama_model="qwen3.5:4b")
        route = resolve_route(TaskKind.SECTION_DRAFT, settings)
        assert route.model == "qwen3.5:4b"
        assert route.source == "default"

    def test_env_override_is_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODEL_ROUTE_SECTION_DRAFT", "qwen3.5:7b")
        settings = Settings(_env_file=None, ollama_model="qwen3.5:4b")
        route = resolve_route(TaskKind.SECTION_DRAFT, settings)
        assert route.model == "qwen3.5:7b"
        assert route.source == "MODEL_ROUTE_SECTION_DRAFT"

    def test_snapshot_lists_every_task(self) -> None:
        settings = Settings(_env_file=None)
        snapshot = routes_snapshot(settings)
        assert {row["task"] for row in snapshot} == {task.value for task in TaskKind}


class TestCacheKey:
    def test_same_request_same_key(self) -> None:
        left = ResponseCache.make_key(
            task="section_draft", provider="p", model="m", request=_request()
        )
        right = ResponseCache.make_key(
            task="section_draft", provider="p", model="m", request=_request()
        )
        assert left == right

    def test_changed_prompt_changes_key(self) -> None:
        left = ResponseCache.make_key(
            task="section_draft", provider="p", model="m", request=_request("раз")
        )
        right = ResponseCache.make_key(
            task="section_draft", provider="p", model="m", request=_request("два")
        )
        assert left != right

    def test_generation_parameters_change_key(self) -> None:
        same = ResponseCache.make_key(
            task="section_draft", provider="p", model="m", request=_request()
        )
        longer = ResponseCache.make_key(
            task="section_draft",
            provider="p",
            model="m",
            request=_request(max_tokens=760),
        )
        assert same != longer

    def test_metadata_does_not_change_key(self) -> None:
        """metadata на ответ не влияет — иначе кэш промахивался бы всегда."""
        left = ResponseCache.make_key(
            task="section_draft",
            provider="p",
            model="m",
            request=_request(metadata={"node": "a"}),
        )
        right = ResponseCache.make_key(
            task="section_draft",
            provider="p",
            model="m",
            request=_request(metadata={"node": "b"}),
        )
        assert left == right


class TestCacheBehaviour:
    async def test_second_call_is_served_from_cache(self) -> None:
        provider = _CountingProvider()
        cache = ResponseCache()
        first = await cache.generate(provider, _request(), task=TaskKind.SECTION_DRAFT)
        second = await cache.generate(provider, _request(), task=TaskKind.SECTION_DRAFT)
        assert provider.calls == 1
        assert second.text == first.text
        assert cache.stats()["hits"] == 1

    async def test_expired_entry_is_recomputed(self) -> None:
        clock = {"now": 0.0}
        provider = _CountingProvider()
        cache = ResponseCache(ttl_seconds=10, clock=lambda: clock["now"])
        await cache.generate(provider, _request(), task=TaskKind.SECTION_DRAFT)
        clock["now"] = 11.0
        await cache.generate(provider, _request(), task=TaskKind.SECTION_DRAFT)
        assert provider.calls == 2

    async def test_lru_eviction_keeps_newest(self) -> None:
        provider = _CountingProvider()
        cache = ResponseCache(max_entries=1)
        await cache.generate(provider, _request("первый"), task=TaskKind.SECTION_DRAFT)
        await cache.generate(provider, _request("второй"), task=TaskKind.SECTION_DRAFT)
        await cache.generate(provider, _request("первый"), task=TaskKind.SECTION_DRAFT)
        assert provider.calls == 3
        assert cache.stats()["entries"] == 1

    async def test_different_task_is_not_a_hit(self) -> None:
        provider = _CountingProvider()
        cache = ResponseCache()
        await cache.generate(provider, _request(), task=TaskKind.SECTION_DRAFT)
        await cache.generate(provider, _request(), task=TaskKind.SECTION_RETRY)
        assert provider.calls == 2

    async def test_returned_copy_does_not_leak_edits(self) -> None:
        provider = _CountingProvider()
        cache = ResponseCache()
        first = await cache.generate(provider, _request(), task=TaskKind.SECTION_DRAFT)
        first.text = "испорчено вызывающим кодом"
        second = await cache.generate(provider, _request(), task=TaskKind.SECTION_DRAFT)
        assert second.text.startswith("ответ-1")


class TestCacheSettings:
    def teardown_method(self) -> None:
        reset_response_cache()

    def test_disabled_cache_is_none(self) -> None:
        settings = Settings(_env_file=None, response_cache_enabled=False)
        assert get_response_cache(settings) is None

    def test_enabled_cache_is_shared(self) -> None:
        settings = Settings(_env_file=None)
        first = get_response_cache(settings)
        second = get_response_cache(settings)
        assert first is not None
        assert first is second
