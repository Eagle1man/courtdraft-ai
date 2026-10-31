"""Кто занял память и какая модель здесь влезет.

Зачем: на машине 8 ГБ «модель не помещается» — это не приговор, а вопрос
«что можно закрыть и какая модель подходит». Без ответа на него
preflight превращается в запрет без выхода.

Скрипт ничего не закрывает и не меняет: только показывает факты и
считает, какая из моделей Ollama влезет в доступную память **с запасом
20%** (тот же запас, что и в preflight — иначе два инструмента давали бы
разные ответы).

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\memory_report.py
    backend\\.venv\\Scripts\\python.exe scripts\\memory_report.py --top 15
"""

from __future__ import annotations

import argparse
import csv
import io
import subprocess  # noqa: S404 - чтение списка процессов системы
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings  # noqa: E402
from app.llm.preflight import (  # noqa: E402
    MEMORY_HEADROOM,
    fetch_ollama_tags,
    gpu_memory_gb,
    system_memory_gb,
)


def top_processes(limit: int) -> list[tuple[str, int, float]]:
    """(имя, pid, ГБ) — по убыванию памяти. Пусто, если способа нет."""
    if sys.platform == "win32":
        try:
            completed = subprocess.run(  # noqa: S603 - фиксированная команда
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                encoding="cp866",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, ValueError):
            return []
        rows: list[tuple[str, int, float]] = []
        for row in csv.reader(io.StringIO(completed.stdout or "")):
            if len(row) < 5:
                continue
            name, pid_raw, memory_raw = row[0], row[1], row[4]
            digits = "".join(ch for ch in memory_raw if ch.isdigit())
            if not digits:
                continue
            # tasklist печатает килобайты с пробелами: «1 234 К».
            rows.append((name, int(pid_raw), int(digits) / 1024**2))
        rows.sort(key=lambda item: item[2], reverse=True)
        return rows[:limit]

    try:
        completed = subprocess.run(  # noqa: S603 - фиксированная команда
            ["ps", "-eo", "rss=,pid=,comm="],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, ValueError):
        return []
    rows = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        rows.append((parts[2], int(parts[1]), int(parts[0]) / 1024**2))
    rows.sort(key=lambda item: item[2], reverse=True)
    return rows[:limit]


def models_that_fit(tags: dict[str, Any], available_gb: float) -> list[tuple[str, float, bool]]:
    result: list[tuple[str, float, bool]] = []
    for item in tags.get("models", []):
        if not isinstance(item, dict):
            continue
        size_gb = float(item.get("size", 0)) / 1024**3
        result.append(
            (str(item.get("name", "")), size_gb, size_gb * MEMORY_HEADROOM <= available_gb)
        )
    result.sort(key=lambda row: row[1])
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    memory = system_memory_gb()
    vram = gpu_memory_gb()
    if memory is None:
        print("объём памяти узнать не удалось")
        return 2
    total_gb, available_gb = memory
    print(f"оперативная память: {available_gb:.1f} ГБ свободно из {total_gb:.1f}")
    if vram is None:
        print("видеопамять: видеокарта NVIDIA не найдена")
    else:
        print(f"видеопамять: {vram[1]:.1f} ГБ свободно из {vram[0]:.1f}")

    print(f"\nкто занимает (топ {args.top}):")
    print(f"{'процесс':<28}{'pid':>8}{'ГБ':>8}")
    for name, pid, gb in top_processes(args.top):
        print(f"{name[:28]:<28}{pid:>8}{gb:>8.2f}")

    settings = Settings()
    print(f"\nмодели Ollama (запас {int((MEMORY_HEADROOM - 1) * 100)}%):")
    try:
        import asyncio

        tags = asyncio.run(fetch_ollama_tags(settings.ollama_base_url))
    except Exception as exc:  # noqa: BLE001 - отчёт не должен падать из-за сети
        print(f"  реестр недоступен: {exc}")
        return 0

    capacity = max(available_gb, vram[1] if vram else 0.0)
    rows = models_that_fit(tags, capacity)
    if not rows:
        print("  реестр пуст")
        return 0
    for name, size_gb, fits in rows:
        mark = "влезет" if fits else "НЕ влезет"
        print(f"  {mark:<10} {name:<52} файл {size_gb:.1f} ГБ")

    fitting = [row for row in rows if row[2]]
    print()
    if fitting:
        biggest = fitting[-1]
        print(
            f"самая крупная подходящая модель: {biggest[0]} "
            f"({biggest[1]:.1f} ГБ, нужно с запасом {biggest[1] * MEMORY_HEADROOM:.1f})"
        )
        print(f"для прогона: OLLAMA_MODEL={biggest[0]}")
    else:
        print("ни одна модель не влезает: закройте тяжёлые программы или возьмите модель меньше")
    return 0


if __name__ == "__main__":
    sys.exit(main())
