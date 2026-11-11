"""Выбор самой дешёвой модели DeepSeek Flash по размеру запроса.

Почему не порог «200/227»: у моделей семейства разная структура цены.
Одна дешевле на входе и дороже на выходе, другая наоборот. Точка
равенства зависит от ОБОИХ размеров:

    tokens_out = tokens_in * (price_in_2 - price_in_1)
                            / (price_out_1 - price_out_2)

При ответе ~30 токенов переключение между V4.1 Flash и V4 Flash 0731
происходит около 200 входных токенов; при ответе 300 токенов — около
2 000. Жёстко зашивать порог нельзя.

Цены не зашиты: список и стоимость берутся из /v1/models провайдера
(ZvenoAI, OpenAI-совместимый) и кэшируются. Если API недоступен, работаем
на модели и ценах из конфига: сервис обязан стартовать и работать в
degraded-режиме (п.53), а не падать.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Модель и её цены в рублях за 1M токенов."""

    id: str
    name: str
    price_in: float
    price_out: float
    price_cache: float = 0.0

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        """Стоимость одного запроса в рублях."""
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000


class ModelRouter:
    """Список моделей с ценами и выбор минимальной стоимости.

    Один экземпляр живёт в провайдере. Кэш обновляется лениво: первый
    вызов после старта или после истечения TTL идёт в сеть, остальные
    берут кэш. Сетевой сбой не стирает уже полученные цены.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_filter: str = "deepseek",
        ttl_sec: int = 21600,
        timeout_sec: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._model_filter = model_filter.strip().lower() or "deepseek"
        self._ttl_sec = max(60, int(ttl_sec))
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._lock = threading.Lock()
        self._items: list[ModelPrice] = []
        self._loaded_at = 0.0
        self._last_error = ""

    @property
    def last_error(self) -> str:
        """Последняя ошибка обновления цен (пустая, если всё хорошо)."""
        with self._lock:
            return self._last_error

    def cached(self) -> list[ModelPrice]:
        """Уже загруженные модели, без сетевого запроса."""
        with self._lock:
            return list(self._items)

    def candidates(self, *, force: bool = False) -> list[ModelPrice]:
        """Модели с ценами. Пустой список — цены недоступны."""
        now = time.monotonic()
        with self._lock:
            fresh = bool(self._items) and now - self._loaded_at < self._ttl_sec
            if fresh and not force:
                return list(self._items)

        try:
            items = self._load()
        except Exception as exc:  # noqa: BLE001 - сеть не должна ронять сервис
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "Не удалось обновить цены моделей: %s", self._last_error
                )
                return list(self._items)

        with self._lock:
            if items:
                self._items = items
                self._loaded_at = now
                self._last_error = ""
            return list(self._items)

    def pick(self, tokens_in: int, tokens_out: int) -> ModelPrice | None:
        """Самая дешёвая модель для такого размера входа и выхода."""
        items = self.candidates()
        if not items:
            return None
        return min(
            items,
            key=lambda item: (item.cost(tokens_in, tokens_out), item.id),
        )

    @staticmethod
    def default(
        model_id: str,
        price_in: float,
        price_out: float,
        price_cache: float = 0.0,
    ) -> ModelPrice:
        """Модель из конфига — когда прайс получить не удалось."""
        return ModelPrice(
            id=model_id,
            name=model_id,
            price_in=price_in,
            price_out=price_out,
            price_cache=price_cache,
        )

    def _load(self) -> list[ModelPrice]:
        payload = self._request_models(self._api_key)
        items = self.parse(payload)
        if not items and self._api_key:
            # Ключ мог быть не принят: публичный список цен не требует
            # авторизации. Пробуем без ключа, а не сдаёмся сразу.
            payload = self._request_models("")
            items = self.parse(payload)
        return items

    def _request_models(self, key: str) -> dict[str, Any]:
        url = f"{self._base_url}/models"
        headers = {
            "Accept": "application/json",
            "User-Agent": "courtdraft-model-router/1.0",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(url, headers=headers)  # noqa: S310
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=self._timeout_sec
        ) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("ответ /models не является JSON-объектом")
        return payload

    def parse(self, payload: dict[str, Any]) -> list[ModelPrice]:
        """Оставить только DeepSeek Flash с положительными ценами."""
        result: list[ModelPrice] = []
        for raw in payload.get("data") or []:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get("id") or "").strip()
            low = model_id.lower()
            if not model_id or model_id.startswith("~"):
                continue
            if "flash" not in low or self._model_filter not in low:
                continue
            if "vision" in low:
                continue
            pricing = raw.get("pricing") or {}
            if not isinstance(pricing, dict):
                continue
            try:
                price_in = float(pricing.get("prompt") or 0) * 1_000_000
                price_out = float(pricing.get("completion") or 0) * 1_000_000
                price_cache = (
                    float(pricing.get("input_cache_read") or 0) * 1_000_000
                )
            except (TypeError, ValueError):
                continue
            if price_in <= 0 or price_out <= 0:
                continue
            result.append(
                ModelPrice(
                    id=model_id,
                    name=str(raw.get("name") or model_id),
                    price_in=price_in,
                    price_out=price_out,
                    price_cache=price_cache,
                )
            )
        return result
