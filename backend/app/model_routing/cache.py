"""Кэш ответов модели (Этап 7).

Зачем: на CPU один раздел генерируется десятки секунд, а повторный прогон
тех же материалов — обычное дело (перегенерация, демо, проверки). Кэш
возвращает ровно тот же `LLMResponse`, поэтому качество не может
ухудшиться по построению: это не «похожий» ответ, а тот же самый.

Ключ учитывает всё, что влияет на ответ: задачу, провайдера, модель,
сообщения и параметры генерации. `LLMRequest.metadata` в ключ не входит:
по контракту она на ответ не влияет — если бы входила, кэш промахивался
бы на каждом узле графа.

Кэш в памяти процесса. Отдельное хранилище (Redis/файлы) здесь не
заводится: система локальная, процесс один, а лишняя зависимость на
ноутбуке 8 ГБ дороже, чем польза от переживания перезапуска.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.llm.base import LLMProvider, LLMRequest, LLMResponse
from app.model_routing.registry import TaskKind


@dataclass
class _Entry:
    response: LLMResponse
    stored_at: float


class ResponseCache:
    """LRU-кэш ответов модели с TTL и счётчиками замера."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds должен быть больше нуля")
        if max_entries <= 0:
            raise ValueError("max_entries должен быть больше нуля")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        # dict сохраняет порядок вставки: первая запись — самая старая.
        self._entries: dict[str, _Entry] = {}
        self._hits = 0
        self._misses = 0
        self._hit_ms_total = 0
        self._miss_ms_total = 0

    @staticmethod
    def make_key(
        *,
        task: str,
        provider: str,
        model: str,
        request: LLMRequest,
    ) -> str:
        payload = {
            "task": task,
            "provider": provider,
            "model": model,
            "role": str(request.role),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "json_mode": request.json_mode,
            "reasoning": request.reasoning,
            "stop": list(request.stop) if request.stop else None,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> LLMResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry.stored_at > self._ttl:
            del self._entries[key]
            return None
        # Переставляем в конец: LRU-вытеснение удаляет самую старую запись.
        self._entries.pop(key)
        self._entries[key] = entry
        return entry.response

    def put(self, key: str, response: LLMResponse) -> None:
        self._entries.pop(key, None)
        self._entries[key] = _Entry(response=response, stored_at=self._clock())
        while len(self._entries) > self._max:
            oldest = next(iter(self._entries))
            del self._entries[oldest]

    async def generate(
        self,
        provider: LLMProvider,
        request: LLMRequest,
        *,
        task: TaskKind,
    ) -> LLMResponse:
        model = getattr(provider, "_model", "") or ""
        key = self.make_key(
            task=task.value,
            provider=provider.name,
            model=model,
            request=request,
        )

        started_hit = time.perf_counter()
        cached = self.get(key)
        if cached is not None:
            self._hits += 1
            self._hit_ms_total += int((time.perf_counter() - started_hit) * 1000)
            # Копия: вызывающий код правит поля ответа, и общий объект
            # переносил бы правку одного раздела в другой.
            return cached.model_copy(deep=True)

        started = time.perf_counter()
        response = await provider.generate(request)
        self._misses += 1
        self._miss_ms_total += int((time.perf_counter() - started) * 1000)
        # В кэш кладётся копия: вызывающий код правит поля ответа, и общий
        # объект переносил бы правку одного раздела в следующий вызов.
        self.put(key, response.model_copy(deep=True))
        return response

    def stats(self) -> dict[str, int]:
        total = self._hits + self._misses
        return {
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "hit_ratio_percent": int(self._hits * 100 / total) if total else 0,
            "hit_avg_ms": int(self._hit_ms_total / self._hits) if self._hits else 0,
            "miss_avg_ms": int(self._miss_ms_total / self._misses) if self._misses else 0,
        }

    def clear(self) -> None:
        self._entries.clear()
        self._hits = self._misses = 0
        self._hit_ms_total = self._miss_ms_total = 0


_singletons: dict[tuple[int, int], ResponseCache] = {}


def get_response_cache(settings: Settings) -> ResponseCache | None:
    """Кэш на процесс или None, если он выключен настройкой."""
    if not settings.response_cache_enabled:
        return None
    shape = (settings.response_cache_ttl_seconds, settings.response_cache_max_entries)
    cache = _singletons.get(shape)
    if cache is None:
        cache = ResponseCache(
            ttl_seconds=shape[0],
            max_entries=shape[1],
        )
        _singletons[shape] = cache
    return cache


def reset_response_cache() -> None:
    """Сбросить кэши (тесты и замер: иначе счётчики переходят между прогонами)."""
    _singletons.clear()
