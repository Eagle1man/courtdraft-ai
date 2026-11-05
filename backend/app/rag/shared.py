"""Один клиент Qdrant на процесс.

Зачем: файловый Qdrant (режим `local`) держит блокировку каталога. Второе
одновременное открытие заканчивается `BlockingIOError`, и запрос тихо
остаётся без норм. Клиент открывался на каждый поиск норм и на каждую
проверку `/api/health` (а её опрашивает каждый открытый браузер раз в
минуту), поэтому столкновение было вопросом времени — и случилось:
формирование проекта падало с 500.

Клиент создаётся один раз и живёт до остановки службы. Это не кэш
ради скорости: повторное открытие того же каталога запрещено самим
хранилищем.
"""

from __future__ import annotations

import contextlib
import threading

from app.config import Settings
from app.rag.store import VectorStore

_store: VectorStore | None = None
_lock = threading.Lock()


def get_store(settings: Settings | None = None) -> VectorStore:
    """Вернуть общий клиент, создав его при первом обращении."""
    global _store
    if _store is not None:
        return _store
    with _lock:
        if _store is None:
            _store = VectorStore.from_settings(settings)
    return _store


def reset_store() -> None:
    """Забыть клиент, не закрывая его.

    Нужно тестам: они поднимают хранилище во временном каталоге, и
    закэшированный клиент от прошлого теста смотрел бы в удалённый путь.
    """
    global _store
    with _lock:
        _store = None


def close_store() -> None:
    """Закрыть клиент при остановке службы."""
    global _store
    with _lock:
        store, _store = _store, None
    if store is not None:
        with contextlib.suppress(Exception):
            store.client.close()
