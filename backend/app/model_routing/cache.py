"""Кэш ответов модели (Этап 7): в памяти и, по настройке, на диске.

Зачем: на CPU один раздел генерируется десятки секунд, а повторный прогон
тех же материалов — обычное дело (перегенерация, демо, проверки). Кэш
возвращает ровно тот же `LLMResponse`, поэтому качество не может
ухудшиться по построению: это не «похожий» ответ, а тот же самый.

Ключ учитывает всё, что влияет на ответ: задачу, провайдера, модель,
сообщения и параметры генерации. `LLMRequest.metadata` в ключ не входит:
по контракту она на ответ не влияет — если бы входила, кэш промахивался
бы на каждом узле графа.

Почему SQLite, а не Redis: система локальная и однопроцессная, а SQLite
уже есть в проекте. Отдельный сервер кэша на ноутбуке 8 ГБ дороже, чем
польза от сохранения нескольких ответов. Файл кэша переживает перезапуск:
именно это и нужно, потому что модель загружается и считает минуты.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.llm.base import LLMProvider, LLMRequest, LLMResponse
from app.model_routing.registry import TaskKind


@dataclass
class _Entry:
    response: LLMResponse
    stored_at: float


class ResponseCache:
    """LRU-кэш ответов модели с TTL, счётчиками и необязательным диском.

    С `path` записи живут в SQLite и переживают перезапуск; без него —
    только в памяти процесса (так ведут себя тесты и стенды).
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        max_entries: int = 128,
        clock: Callable[[], float] = time.time,
        path: Path | None = None,
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
        self._db: sqlite3.Connection | None = None
        if path is not None:
            self._open(path)
        self._hits = 0
        self._misses = 0
        self._hit_ms_total = 0
        self._miss_ms_total = 0

    # ── Хранилище ─────────────────────────────────────────────────────

    def _open(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: один процесс, но корутины могут
        # выполняться в разных потоках event loop.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            "key TEXT PRIMARY KEY, payload TEXT NOT NULL, stored_at REAL NOT NULL)"
        )
        self._db.commit()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __del__(self) -> None:  # pragma: no cover - защита от утечки файла
        # В деструкторе молчим: исключение здесь некому обработать.
        with suppress(Exception):
            self.close()

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
        if self._db is not None:
            return self._get_from_db(key)

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

    def _get_from_db(self, key: str) -> LLMResponse | None:
        assert self._db is not None
        row = self._db.execute(
            "SELECT payload, stored_at FROM responses WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        payload, stored_at = row
        if self._clock() - float(stored_at) > self._ttl:
            self._db.execute("DELETE FROM responses WHERE key = ?", (key,))
            self._db.commit()
            return None
        try:
            return LLMResponse.model_validate_json(str(payload))
        except ValueError:
            # Испорченная запись (обрезанный файл, смена формата) — не
            # повод падать: удаляем и считаем промахом.
            self._db.execute("DELETE FROM responses WHERE key = ?", (key,))
            self._db.commit()
            return None

    def put(self, key: str, response: LLMResponse) -> None:
        if self._db is not None:
            self._put_to_db(key, response)
            return
        self._entries.pop(key, None)
        self._entries[key] = _Entry(response=response, stored_at=self._clock())
        while len(self._entries) > self._max:
            oldest = next(iter(self._entries))
            del self._entries[oldest]

    def _put_to_db(self, key: str, response: LLMResponse) -> None:
        assert self._db is not None
        now = self._clock()
        self._db.execute(
            "INSERT OR REPLACE INTO responses (key, payload, stored_at) "
            "VALUES (?, ?, ?)",
            (key, response.model_dump_json(), now),
        )
        # Истёкшие записи не храним: иначе файл растёт без пользы.
        self._db.execute(
            "DELETE FROM responses WHERE stored_at < ?", (now - self._ttl,)
        )
        excess = self._entries_count() - self._max
        if excess > 0:
            self._db.execute(
                "DELETE FROM responses WHERE key IN ("
                "SELECT key FROM responses ORDER BY stored_at ASC LIMIT ?)",
                (excess,),
            )
        self._db.commit()

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

    def _entries_count(self) -> int:
        if self._db is not None:
            assert self._db is not None
            row = self._db.execute("SELECT COUNT(*) FROM responses").fetchone()
            return int(row[0]) if row else 0
        return len(self._entries)

    def stats(self) -> dict[str, int]:
        total = self._hits + self._misses
        return {
            "entries": self._entries_count(),
            "hits": self._hits,
            "misses": self._misses,
            "hit_ratio_percent": int(self._hits * 100 / total) if total else 0,
            "hit_avg_ms": int(self._hit_ms_total / self._hits) if self._hits else 0,
            "miss_avg_ms": int(self._miss_ms_total / self._misses) if self._misses else 0,
        }

    def clear(self) -> None:
        if self._db is not None:
            assert self._db is not None
            self._db.execute("DELETE FROM responses")
            self._db.commit()
        self._entries.clear()
        self._hits = self._misses = 0
        self._hit_ms_total = self._miss_ms_total = 0


_singletons: dict[tuple[int, int, str], ResponseCache] = {}


def get_response_cache(settings: Settings) -> ResponseCache | None:
    """Кэш на процесс (при заданном пути — общий файл) или None."""
    if not settings.response_cache_enabled:
        return None
    path = Path(settings.response_cache_path) if settings.response_cache_path else None
    shape = (
        settings.response_cache_ttl_seconds,
        settings.response_cache_max_entries,
        path.as_posix() if path else "",
    )
    cache = _singletons.get(shape)
    if cache is None:
        cache = ResponseCache(
            ttl_seconds=shape[0],
            max_entries=shape[1],
            path=path,
        )
        _singletons[shape] = cache
    return cache


def reset_response_cache() -> None:
    """Сбросить кэши (тесты и замер: иначе счётчики переходят между прогонами)."""
    for cache in _singletons.values():
        cache.close()
    _singletons.clear()
