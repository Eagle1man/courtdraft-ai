"""Итоговые испытания (Этап 10): один прогон — все проверки.

Смысл: воспроизводимость должна быть командой, а не пересказом. Скрипт
запускает то, чем проект проверялся, сохраняет фактические коды возврата
и последние строки вывода в `docs/final-trials.json`. Отчёт
`docs/final-trials.md` пишется по этим числам, а не по памяти.

Что входит:
- полный pytest;
- ruff и mypy (strict) по backend;
- benchmark детерминированных проверок;
- benchmark Этапа 6 (пары «материалы → эталонное решение»);
- замер кэша Этапа 7.

Чего здесь нет и почему:
- REAL-прогон модели: на этой машине (8 ГБ, CPU-only) он не выполнялся,
  поэтому цифр качества реальной модели здесь нет и выдумывать их нельзя;
- браузерные проверки (`ui_claims_check.py`, `ui_reverify_check.py`):
  им нужен поднятый демо-стенд, запускаются отдельно;
- Этап 6 воспроизводится только при наличии `data/demo/real/` — эти
  материалы не в репозитории (персональные данные/лицензия), поэтому на
  чистом клоне шаг честно упадёт, и это записано в ограничениях.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\final_trials.py
"""

from __future__ import annotations

import json
import platform
import subprocess  # noqa: S404 - запуск собственных проверок проекта
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"
RUFF = BACKEND / ".venv" / "Scripts" / "ruff.exe"
MYPY = BACKEND / ".venv" / "Scripts" / "mypy.exe"

CHECKS: list[dict[str, object]] = [
    {
        "name": "pytest",
        "command": [str(VENV_PYTHON), "-m", "pytest", "-q"],
        "cwd": BACKEND,
        "tail": 2,
    },
    {
        "name": "ruff",
        "command": [str(RUFF), "check", "app", "tests"],
        "cwd": BACKEND,
        "tail": 2,
    },
    {
        "name": "mypy",
        "command": [str(MYPY), "app"],
        "cwd": BACKEND,
        "tail": 2,
    },
    {
        "name": "benchmark_rules",
        "command": [str(VENV_PYTHON), "../scripts/verification_benchmark.py"],
        "cwd": BACKEND,
        "tail": 12,
    },
    {
        "name": "benchmark_stage6",
        "command": [str(VENV_PYTHON), "../scripts/stage6_benchmark.py"],
        "cwd": BACKEND,
        "tail": 10,
    },
    {
        "name": "cache_stage7",
        "command": [str(VENV_PYTHON), "scripts/stage7_routing_benchmark.py"],
        "cwd": ROOT,
        "tail": 6,
    },
]


def run_check(check: dict[str, object]) -> dict[str, object]:
    command = [str(part) for part in check["command"]]
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603 - команды заданы в этом файле
        command,
        cwd=str(check["cwd"]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    elapsed = round(time.perf_counter() - started, 1)
    output = (completed.stdout or "").strip().splitlines()
    tail = int(check["tail"])
    return {
        "name": check["name"],
        "command": " ".join(command),
        "cwd": str(check["cwd"]),
        "exit_code": completed.returncode,
        "seconds": elapsed,
        "output_tail": output[-tail:],
        "stderr_tail": (completed.stderr or "").strip().splitlines()[-3:],
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    results = [run_check(check) for check in CHECKS]
    report = {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "checks": results,
    }
    out = ROOT / "docs" / "final-trials.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    failed: list[str] = []
    for result in results:
        mark = "OK  " if result["exit_code"] == 0 else "FAIL"
        last = (result["output_tail"] or [""])[-1]
        print(f'[{mark}] {result["name"]:<18} {result["seconds"]:>6.1f} с  {last}')
        if result["exit_code"] != 0:
            failed.append(str(result["name"]))

    print(f"Отчёт: {out}")
    if failed:
        print("НЕ ПРОШЛО: " + ", ".join(failed))
        return 1
    print("ПРОШЛО: все проверки с кодом 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
