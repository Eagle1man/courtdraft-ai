"""Проверка готовности REAL-режима (Ollama) перед прогоном.

Зачем отдельный модуль, а не «запустим и посмотрим»: отказ REAL выглядит
как пустой раздел в проекте, то есть неотличим от «модель промолчала по
существу». На экране это один и тот же результат, а причины разные:
сервер не запущен, модели нет в реестре, не хватает памяти, модель ушла
в размышление и вернула пустой content.

Проверки разделены по смыслу и **не скрывают неопределённость**:

- `provider` — включён ли REAL в настройках (MOCK отвечает «не готово»,
  а не «всё хорошо»);
- `ollama` — сервер отвечает; отдельно «нет связи» и «ответил ошибкой»;
- `model` — нужная модель есть в реестре (иначе — список того, что есть);
- `memory` — поместится ли модель в доступную память;
- `answer` — необязательный пробный запрос: модель вернула текст, а не
  пустой content.

Модуль ничего не меняет в системе: только читает настройки, реестр и
память. Пробный запрос выполняется только если его попросили.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

import httpx

from app.config import EmbeddingProviderName, LLMProviderName, Settings
from app.llm.base import LLMError, LLMMessage, LLMRequest, LLMRole
from app.llm.ollama_provider import OllamaProvider

GB = 1024**3
# Размер файла модели — нижняя граница: при запуске нужны ещё KV-кэш и
# контекст (num_ctx 4096). Запас 20% не даёт объявить «поместится» там,
# где модель влезает ровно впритык и падает на первом длинном разделе.
MEMORY_HEADROOM = 1.2


class CheckState(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    state: CheckState
    detail: str
    hint: str = ""


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Готово, только если ни одна проверка не провалена.

        WARN не блокирует запуск: предупреждение адресовано человеку,
        а не программе (например, не хватает модели эмбеддингов — система
        обязана работать в деградации, а не отказываться стартовать).
        """
        return not any(check.state is CheckState.FAIL for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [
                {
                    "name": check.name,
                    "state": check.state.value,
                    "detail": check.detail,
                    "hint": check.hint,
                }
                for check in self.checks
            ],
        }


async def fetch_ollama_tags(base_url: str, timeout_sec: float = 5.0) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        response = await client.get(f"{base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        return cast("dict[str, Any]", response.json())


def gpu_memory_gb() -> tuple[float, float] | None:
    """(всего VRAM ГБ, свободно VRAM ГБ) или None, если видеокарты нет.

    Ollama раскладывает слои модели на GPU, и модель, которая не влезает
    в оперативную память, спокойно живёт в видеопамяти RTX. Поэтому
    «модель больше свободной RAM» — ещё не приговор; проверка обязана
    смотреть на VRAM, иначе честный запуск на 3050 был бы заблокирован.
    """
    import shutil
    import subprocess  # noqa: S404 - чтение состояния драйвера

    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - фиксированная команда
            [
                executable,
                "--query-gpu=memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError):
        return None
    line = (completed.stdout or "").strip().splitlines()
    if completed.returncode != 0 or not line:
        return None
    parts = [part.strip() for part in line[0].split(",")]
    if len(parts) != 2:
        return None
    try:
        total_mb, free_mb = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return total_mb / 1024, free_mb / 1024


def system_memory_gb() -> tuple[float, float] | None:
    """(всего ГБ, доступно ГБ) или None, если узнать не удалось.

    Отдельный None вместо нуля: «неизвестно» и «0 ГБ свободно» — разные
    факты, и второй означал бы гарантированный отказ.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.ullTotalPhys / GB, status.ullAvailPhys / GB
        except (AttributeError, OSError, ValueError):
            return None

    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page / GB
        available = os.sysconf("SC_AVPHYS_PAGES") * page / GB
    except (AttributeError, OSError, ValueError):
        return None
    return total, available


async def default_probe(settings: Settings) -> tuple[bool, str]:
    """Один короткий запрос к модели: ответ есть и он текст, а не пусто."""
    provider = OllamaProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_sec=settings.ollama_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        temperature=settings.ollama_temperature,
    )
    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content="Ответь одной короткой фразой по-русски."),
            LLMMessage(role="user", content="Назови текущий день недели."),
        ],
        role=LLMRole.GENERATION,
        max_tokens=32,
        reasoning=False,
    )
    try:
        response = await provider.generate(request)
    except LLMError as exc:
        return False, f"модель не ответила: {exc}"

    text = response.text.strip()
    if not text:
        return False, (
            "модель ответила пустым текстом (пустой content: вероятно, "
            "ответ ушёл в размышление)"
        )
    return True, f"ответ {len(text)} символов за {response.latency_ms} мс"


def _model_entry(tags: dict[str, Any], model: str) -> dict[str, Any] | None:
    """Найти модель в реестре: сначала точное имя, затем по имени без тега.

    «qwen3.5:4b» и «qwen3.5:4b» — одно и то же, а «qwen3.5:4b» и
    «qwen3.5:7b» — разные модели. Подставлять вторую вместо первой нельзя:
    разница в памяти и качестве, и отчёт об этом соврёт.
    """
    models = [item for item in tags.get("models", []) if isinstance(item, dict)]
    for item in models:
        if str(item.get("name", "")) == model:
            return item
    if ":" in model:
        # Тег указан явно — подставлять другую версию нельзя: «4b» вместо
        # «7b» отличается памятью и качеством, и отчёт об этом соврёт.
        return None
    for item in models:
        if str(item.get("name", "")).split(":")[0] == model:
            return item
    return None


async def run_preflight(
    settings: Settings,
    *,
    fetch_tags: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    memory: Callable[[], tuple[float, float] | None] = system_memory_gb,
    gpu: Callable[[], tuple[float, float] | None] = gpu_memory_gb,
    probe: Callable[[Settings], Awaitable[tuple[bool, str]]] | None = None,
) -> PreflightReport:
    """Собрать отчёт о готовности. Ничего не меняет."""
    report = PreflightReport()
    fetch = fetch_tags or (lambda url: fetch_ollama_tags(url))

    if settings.llm_provider is not LLMProviderName.OLLAMA:
        report.checks.append(
            Check(
                name="provider",
                state=CheckState.FAIL,
                detail=f"LLM_PROVIDER={settings.llm_provider.value}: это не REAL",
                hint=(
                    "для прогона реальной модели: LLM_PROVIDER=ollama, "
                    "OLLAMA_ENABLED=true, APP_ENV=runtime"
                ),
            )
        )
    else:
        report.checks.append(
            Check(
                name="provider",
                state=CheckState.OK,
                detail=f"LLM_PROVIDER=ollama, модель {settings.ollama_model}",
            )
        )

    if not settings.ollama_enabled:
        report.checks.append(
            Check(
                name="ollama_enabled",
                state=CheckState.FAIL,
                detail="OLLAMA_ENABLED=false при LLM_PROVIDER=ollama",
                hint="поставить OLLAMA_ENABLED=true в .env",
            )
        )

    tags: dict[str, Any] | None = None
    try:
        tags = await fetch(settings.ollama_base_url)
    except httpx.HTTPStatusError as exc:
        report.checks.append(
            Check(
                name="ollama",
                state=CheckState.FAIL,
                detail=f"Ollama ответила {exc.response.status_code}: {exc.response.text[:120]}",
                hint="это не «модели нет», а отказ сервера: смотреть лог Ollama",
            )
        )
    except httpx.HTTPError as exc:
        report.checks.append(
            Check(
                name="ollama",
                state=CheckState.FAIL,
                detail=f"нет связи с {settings.ollama_base_url}: {exc}",
                hint="запустить `ollama serve` и проверить адрес в .env",
            )
        )
    else:
        report.checks.append(
            Check(
                name="ollama",
                state=CheckState.OK,
                detail=f"сервер отвечает: {settings.ollama_base_url}",
            )
        )

    entry: dict[str, Any] | None = None
    if tags is not None:
        entry = _model_entry(tags, settings.ollama_model)
        if entry is None:
            available = ", ".join(
                str(item.get("name", "")) for item in tags.get("models", [])
            ) or "реестр пуст"
            report.checks.append(
                Check(
                    name="model",
                    state=CheckState.FAIL,
                    detail=f"модели {settings.ollama_model} нет в реестре",
                    hint=f"`ollama pull {settings.ollama_model}`; сейчас есть: {available}",
                )
            )
        else:
            report.checks.append(
                Check(
                    name="model",
                    state=CheckState.OK,
                    detail=f"модель {entry.get('name', settings.ollama_model)} в реестре",
                )
            )

    sizes = memory()
    vram = gpu()
    model_gb = float(entry.get("size", 0)) / GB if entry is not None else 0.0
    needed_gb = model_gb * MEMORY_HEADROOM if model_gb else 0.0
    if entry is None:
        pass
    elif model_gb and vram is not None and vram[1] >= needed_gb:
        report.checks.append(
            Check(
                name="memory",
                state=CheckState.OK,
                detail=(
                    f"модель {model_gb:.1f} ГБ помещается в видеопамять: "
                    f"свободно {vram[1]:.1f} из {vram[0]:.1f} ГБ, "
                    f"с запасом нужно {needed_gb:.1f}"
                ),
            )
        )
    elif model_gb and sizes is not None and sizes[1] >= needed_gb:
        report.checks.append(
            Check(
                name="memory",
                state=CheckState.WARN,
                detail=(
                    f"модель {model_gb:.1f} ГБ пойдёт на CPU: свободной памяти "
                    f"{sizes[1]:.1f} ГБ из {sizes[0]:.1f}"
                ),
                hint="запустится, но генерация раздела займёт минуты",
            )
        )
    elif model_gb and (sizes is not None or vram is not None):
        vram_part = (
            f"{vram[1]:.1f} ГБ" if vram is not None else "нет (видеокарта не найдена)"
        )
        ram_part = f"{sizes[1]:.1f} ГБ" if sizes is not None else "неизвестно"
        report.checks.append(
            Check(
                name="memory",
                state=CheckState.FAIL,
                detail=(
                    f"модель {model_gb:.1f} ГБ, с запасом нужно "
                    f"{needed_gb:.1f}: свободно VRAM {vram_part}, RAM {ram_part}"
                ),
                hint=(
                    "взять модель меньше (4b вместо 7b), освободить память "
                    "или перенести прогон на машину с большей памятью; "
                    "что именно влезает здесь — python scripts\\memory_report.py"
                ),
            )
        )
    elif model_gb:
        report.checks.append(
            Check(
                name="memory",
                state=CheckState.WARN,
                detail="объём памяти узнать не удалось",
                hint="проверить вручную: модель должна влезать в память или VRAM",
            )
        )

    if settings.embedding_provider is EmbeddingProviderName.LOCAL:
        report.checks.append(
            Check(
                name="embeddings",
                state=CheckState.WARN,
                detail=f"EMBEDDING_PROVIDER=local, модель {settings.embedding_model}",
                hint=(
                    "если модели нет, система работает в деградации — "
                    "это допустимо, но поиск по смыслу будет отключён"
                ),
            )
        )

    if probe is not None:
        ok, detail = await probe(settings)
        report.checks.append(
            Check(
                name="answer",
                state=CheckState.OK if ok else CheckState.FAIL,
                detail=detail,
                hint="" if ok else "модель загружена, но не отвечает текстом",
            )
        )

    return report


def main() -> int:
    """Запуск из командной строки: `python -m app.llm.preflight`."""
    if hasattr(sys.stdout, "reconfigure"):
        # Консоль Windows в cp1251 не печатает «→» и кириллицу в ряде
        # случаев; отчёт не должен падать из-за кодировки вывода.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings()
    probe = default_probe if "--probe" in sys.argv else None
    report = asyncio.run(run_preflight(settings, probe=probe))
    for check in report.checks:
        print(f"[{check.state.value:<4}] {check.name:<14} {check.detail}")
        if check.hint:
            print(f"       -> {check.hint}")
    print("ГОТОВО" if report.ready else "НЕ ГОТОВО")
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
