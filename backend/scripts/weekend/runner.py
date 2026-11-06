"""Оркестратор выходного прогона на простаивающем стенде.

Что делает:

1. Ждёт, пока закончит свой прогон скрипт 7B (`run_7b_after_queue.py`):
   два тяжёлых прогона одновременно на четырёх ядрах только мешают.
2. Дожидается пустой очереди заданий: тяжёлые замеры не должны
   отнимать процессор у студентов, если те ещё работают.
3. Выполняет задачи по очереди, сохраняя результат каждой в
   `state.json`. Прерванный прогон продолжается с того же места.
4. Собирает `report.md` и `report.json`.

Остановка: файл `STOP` рядом с отчётом. Текущая задача доработает,
следующие не начнутся.

Запуск на стенде:
    cd backend && .venv/bin/python -m scripts.weekend.runner --out ../data/weekend
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from scripts.weekend.tasks import TASKS

ROOT = Path(__file__).resolve().parents[2]  # backend/
DB = ROOT.parent / "data/cases/courtdraft.db"
BACKUPS = ROOT.parent / "data/cases/backups"
BASE_URL = "http://127.0.0.1:8081/api"

#: Сколько ждать завершения прогона 7B и очередь студентов.
WAIT_7B_HOURS = 12
WAIT_IDLE_HOURS = 6


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def stopped(out: Path) -> bool:
    return (out / "STOP").exists()


def jobs_active() -> int:
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
        try:
            return con.execute(
                "select count(*) from jobs where state in ('PENDING','RUNNING')"
            ).fetchone()[0]
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - база занята, проверим позже
        return 1


def seven_b_running() -> bool:
    """Идёт ли ещё прогон 7B. `pgrep` — системная утилита Linux."""
    result = subprocess.run(
        ["pgrep", "-f", "run_7b_after_queue"],  # noqa: S607 - системная утилита
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def wait(condition, *, hours: float, label: str, out: Path) -> bool:
    deadline = time.time() + hours * 3600
    while time.time() < deadline:
        if stopped(out):
            log("найден STOP — выходим")
            return False
        if condition():
            log(f"дождались: {label}")
            return True
        time.sleep(120)
    log(f"не дождались: {label}")
    return False


def load_state(out: Path) -> dict:
    path = out / "state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"tasks": {}}


def save_state(out: Path, state: dict) -> None:
    (out / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_report(out: Path, state: dict) -> None:
    """Собрать человекочитаемый отчёт по всем задачам."""
    lines = [
        "# Выходной прогон стенда",
        "",
        f"Собрано: {datetime.now():%d.%m.%Y %H:%M}",
        "",
    ]
    for name, item in state["tasks"].items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"Состояние: **{item.get('status')}**, {item.get('minutes', 0)} мин")
        result = item.get("result") or {}
        if name == "tests":
            lines.append(f"- pytest: код {result.get('pytest_code')}, {result.get('pytest_tail')}")
            lines.append(f"- ruff: код {result.get('ruff_code')}, {result.get('ruff_tail')}")
        elif name == "integrity":
            lines.append(f"- целостность: {result.get('integrity')}")
            lines.append(f"- нарушений внешних ключей: {result.get('foreign_key_violations')}")
            lines.append(f"- сироты: {result.get('orphans')}")
            lines.append(f"- удалено старых копий: {result.get('backups_removed')}")
            lines.append(f"- записи: {result.get('counts')}")
        elif name == "extract_bench":
            lines.append(
                f"- модель: `{result.get('model')}`, "
                f"документов: {result.get('documents')}"
            )
            lines.append(
                f"- без замечаний: {result.get('ok')}, требуют проверки: "
                f"{result.get('needs_review')}, таймаутов: {result.get('timeouts')}"
            )
            lines.append(
                f"- суммы: {result.get('amounts')}, даты: {result.get('dates')}, "
                f"требования: {result.get('claims')}"
            )
            lines.append(
                f"- цитаты: точных {result.get('quotes_exact')}, "
                f"не найдено {result.get('quotes_absent')}"
            )
            lines.append(f"- среднее время на документ: {result.get('avg_seconds')} с")
        elif name == "fuzz":
            lines.append(f"- проверок: {len(result.get('checks', []))}, "
                         f"прошло {result.get('passed')}, провалилось {result.get('failed')}")
            for check in result.get("checks", []):
                mark = "OK" if check["ok"] else "ПРОВАЛ"
                lines.append(
                    f"  - {check['file']}: {check['status']} "
                    f"(ожидалось {check['expected']}) — {mark}"
                )
        elif name == "perf":
            for endpoint, metric in result.items():
                if isinstance(metric, dict):
                    lines.append(
                        f"- `{endpoint}`: p50 {metric.get('p50_ms')} мс, "
                        f"p95 {metric.get('p95_ms')} мс, коды {metric.get('codes')}"
                    )
        lines.append("")

    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "report.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT.parent / "data/weekend")
    parser.add_argument("--only", default="", help="список задач через запятую")
    parser.add_argument("--force", action="store_true", help="пересчитать сделанные")
    args = parser.parse_args()

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    state = load_state(out)

    names = [name.strip() for name in args.only.split(",") if name.strip()] or list(TASKS)
    unknown = [name for name in names if name not in TASKS]
    if unknown:
        raise SystemExit(f"неизвестные задачи: {', '.join(unknown)}")

    if seven_b_running():
        log("прогон 7B ещё идёт — ждём его окончания")
        finished = wait(
            lambda: not seven_b_running(),
            hours=WAIT_7B_HOURS,
            label="конец прогона 7B",
            out=out,
        )
        if not finished:
            return

    for name in names:
        if stopped(out):
            log("STOP — завершаем")
            break
        done = state["tasks"].get(name, {})
        if done.get("status") == "done" and not args.force:
            log(f"{name}: уже сделано, пропускаем")
            continue

        if name in {"extract_bench", "perf", "tests"}:
            idle = wait(
                lambda: jobs_active() == 0,
                hours=WAIT_IDLE_HOURS,
                label="пустая очередь заданий",
                out=out,
            )
            if not idle:
                break

        log(f"{name}: старт")
        started = time.time()
        try:
            result = TASKS[name](
                backend=ROOT,
                db_path=DB,
                backups_dir=BACKUPS,
                output=out,
                base_url=BASE_URL,
            )
            status = "done"
        except Exception as exc:  # noqa: BLE001
            log(f"{name}: ОШИБКА {type(exc).__name__}: {exc}")
            result = {"error": f"{type(exc).__name__}: {exc}"[:500]}
            status = "failed"

        minutes = round((time.time() - started) / 60, 1)
        state["tasks"][name] = {
            "status": status,
            "minutes": minutes,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "result": result,
        }
        save_state(out, state)
        build_report(out, state)
        log(f"{name}: {status} за {minutes} мин")

    build_report(out, state)
    log(f"готово. Отчёт: {out / 'report.md'}")


if __name__ == "__main__":
    sys.exit(main())
