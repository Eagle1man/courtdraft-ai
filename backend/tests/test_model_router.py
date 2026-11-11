"""Выбор модели DeepSeek Flash по размеру запроса.

Здесь деньги: неверный выбор удорожает каждый ответ. Тест подменяет
список моделей и проверяет формулу, а не конкретный прайс Zveno —
иначе тест ломался бы при каждом изменении цен.
"""
from __future__ import annotations

import pytest

from app.llm.model_router import ModelPrice, ModelRouter

FAKE = [
    ModelPrice(
        "deepseek/deepseek-v4.1-flash",
        "V4.1",
        1.1599037,
        77.3269157,
        0.2319807,
    ),
    ModelPrice(
        "deepseek/deepseek-v4-flash-0731",
        "V4 0731",
        8.3899704,
        27.0644205,
        2.706442,
    ),
]


def make_router() -> ModelRouter:
    return ModelRouter(base_url="https://example.invalid/v1", api_key="")


def test_short_answer_uses_cheap_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Короткая реплика: выгоднее модель с дешёвым входом."""
    router = make_router()
    monkeypatch.setattr(router, "candidates", lambda: FAKE)
    assert router.pick(200, 20) == FAKE[0]


def test_long_answer_uses_cheap_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Длинный разбор: выгоднее модель с дешёвым выходом."""
    router = make_router()
    monkeypatch.setattr(router, "candidates", lambda: FAKE)
    assert router.pick(1600, 300) == FAKE[1]


def test_break_even_is_about_143_tokens_out_per_1000_in() -> None:
    """Точка равенства: выход ≈ 14,4% от входа."""
    for tokens_in in (500, 1000, 2000):
        equal = int(tokens_in * 0.1439)
        cheap_a = FAKE[0].cost(tokens_in, equal + 50)
        cheap_b = FAKE[1].cost(tokens_in, equal + 50)
        assert cheap_b < cheap_a
        cheap_a = FAKE[0].cost(tokens_in, max(1, equal - 50))
        cheap_b = FAKE[1].cost(tokens_in, max(1, equal - 50))
        assert cheap_a < cheap_b


def test_cost_formula() -> None:
    expected = (1000 * 1.1599037 + 100 * 77.3269157) / 1_000_000
    assert FAKE[0].cost(1000, 100) == pytest.approx(expected)


def test_parse_excludes_aliases_and_vision() -> None:
    router = make_router()
    payload = {
        "data": [
            {
                "id": "~deepseek/deepseek-flash-latest",
                "pricing": {"prompt": "0.1", "completion": "0.2"},
            },
            {
                "id": "deepseek/deepseek-v4-flash-vision-exp",
                "pricing": {"prompt": "1", "completion": "2"},
            },
            {
                "id": "deepseek/deepseek-v4.1-flash-1017",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
        ]
    }
    parsed = router.parse(payload)
    assert [item.id for item in parsed] == ["deepseek/deepseek-v4.1-flash-1017"]


def test_empty_price_list_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = make_router()
    monkeypatch.setattr(router, "candidates", lambda: [])
    assert router.pick(1000, 300) is None
    fallback = router.default("fallback/model", 1.0, 2.0)
    assert fallback.id == "fallback/model"
    assert fallback.cost(1_000_000, 1) == pytest.approx(1.000002)


def test_refresh_returns_new_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    router = make_router()
    calls = {"n": 0}

    def fake_request(key: str) -> dict:
        calls["n"] += 1
        price = "0.000001" if calls["n"] == 1 else "0.000002"
        return {
            "data": [
                {
                    "id": "deepseek/deepseek-v4-flash-0731",
                    "pricing": {"prompt": price, "completion": "0.000003"},
                }
            ]
        }

    monkeypatch.setattr(router, "_request_models", fake_request)
    first = router.candidates(force=True)
    second = router.candidates(force=True)
    assert first[0].price_in == pytest.approx(1.0)
    assert second[0].price_in == pytest.approx(2.0)


def test_api_failure_keeps_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    router = make_router()
    calls = {"n": 0}

    def fake_request(key: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "data": [
                    {
                        "id": "deepseek/deepseek-v4-flash-0731",
                        "pricing": {
                            "prompt": "0.000001",
                            "completion": "0.000002",
                        },
                    }
                ]
            }
        raise OSError("network down")

    monkeypatch.setattr(router, "_request_models", fake_request)
    first = router.candidates(force=True)
    second = router.candidates(force=True)
    assert first == second
    assert "network down" in router.last_error
