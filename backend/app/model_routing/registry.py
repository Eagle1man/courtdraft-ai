"""Маршрутизация задач к моделям (Этап 7).

Маршрут отвечает на вопрос «какая модель решает эту задачу». Пока модель
одна, маршрут совпадает с `OLLAMA_MODEL`, и поведение системы не
меняется: маршрутизация введена как точка расширения, а не как смена
модели «на всякий случай». Переопределение — переменной окружения
`MODEL_ROUTE_<ЗАДАЧА>`, например `MODEL_ROUTE_SECTION_DRAFT`.

Почему не в `.env` через Settings: список задач расширяется кодом, а
`Settings` с `extra="ignore"` молча проглотил бы опечатку в имени
переменной. Здесь неизвестное имя задачи просто не запрашивается.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from app.config import Settings

ENV_PREFIX = "MODEL_ROUTE_"


class TaskKind(StrEnum):
    """Задачи, для которых маршрут имеет смысл.

    Дробим только там, где разница реальна: первый запрос раздела и
    повторный (с якорем) отличаются лимитом токенов, и на маленькой
    модели это разные режимы.
    """

    SECTION_DRAFT = "section_draft"
    SECTION_RETRY = "section_retry"


@dataclass(frozen=True)
class ModelRoute:
    """Выбранная модель и откуда она взялась."""

    task: TaskKind
    model: str
    source: str


def resolve_route(task: TaskKind, settings: Settings) -> ModelRoute:
    name = ENV_PREFIX + task.value.upper()
    override = os.environ.get(name, "").strip()
    if override:
        return ModelRoute(task=task, model=override, source=name)
    return ModelRoute(task=task, model=settings.ollama_model, source="default")


def routes_snapshot(settings: Settings) -> list[dict[str, str]]:
    """Что и куда маршрутизируется — для замера и диагностики."""
    return [
        {
            "task": route.task.value,
            "model": route.model,
            "source": route.source,
        }
        for route in (resolve_route(task, settings) for task in TaskKind)
    ]
