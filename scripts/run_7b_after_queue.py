#!/usr/bin/env python3
"""Дождаться конца очереди, включить 7B и прогнать те же дела заново.

Зачем: очередь на стенде идёт на qwen2.5:1.5b, а сравнить с сильной
моделью нужно на тех же самых делах. Скрипт снимает список дел,
которые сейчас ждут в очереди, ждёт, пока она опустеет (перезапуск
посреди работы убил бы задание), включает 7B, прогоняет эти дела
по очереди и возвращает быстрый режим.

Остановка в любой момент: `touch STOP_7B` в корне проекта.
После прогона стенд возвращается к маршрутизации на 1.5B — утром
студенты не должны ждать по полчаса из-за 7B.

Запуск: nohup python3 run_7b_after_queue.py > data/7b_run.log 2>&1 &
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE = "http://127.0.0.1:8081/api"
ROOT = "/home/ubuntuii/courtdraft-ai"
DB = f"{ROOT}/data/cases/courtdraft.db"
ENV = f"{ROOT}/.env"
STOP = f"{ROOT}/STOP_7B"
REPORT = f"{ROOT}/data/7b_report.json"

WAIT_IDLE_LIMIT_H = 6
CASE_TIMEOUT_MIN = 240
MAX_CONSECUTIVE_FAILURES = 3
#: Сколько дел гонять на 7B. На этом процессоре один проект идёт часы,
#: и полтора десятка дел не успеют до утра — для сравнения хватает трёх.
MAX_CASES = int(os.environ.get("MAX_7B_CASES", "3"))
#: Список дел, снятый до начала ожидания. Нужен, потому что к моменту
#: переключения очередь уже пуста: задания выполняются по одному.
CASES_FILE = f"{ROOT}/data/7b_cases.txt"


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def query(sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def stopped() -> bool:
    return os.path.exists(STOP)


def pending_cases() -> list[str]:
    rows = query(
        "select distinct case_id from jobs "
        "where kind='draft' and state='PENDING' order by created_at"
    )
    return [row[0] for row in rows]


def active_jobs() -> int:
    return query(
        "select count(*) from jobs where state in ('PENDING','RUNNING')"
    )[0][0]


def case_number(case_id: str) -> str:
    rows = query("select case_number from cases where id=?", (case_id,))
    return rows[0][0] if rows else case_id[:8]


def http(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def wait_health(limit_sec: int = 180) -> bool:
    deadline = time.time() + limit_sec
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=5) as response:
                if '"status":"OK"' in response.read().decode():
                    return True
        except Exception:  # noqa: BLE001 - служба поднимается
            pass
        time.sleep(3)
    return False


def set_env(key: str, value: str) -> None:
    """Заменить строку KEY=... в .env, сохранив остальное как есть."""
    with open(ENV, encoding="utf-8") as handle:
        lines = handle.readlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}\n"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}\n")
    with open(ENV, "w", encoding="utf-8") as handle:
        handle.writelines(lines)


def _env_value(key: str) -> str:
    """Текущее значение настройки из .env (без библиотеки настроек)."""
    try:
        with open(ENV, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        return ""
    return ""


def restart_backend() -> bool:
    """Перезапустить службу так же, как это делает деплой: kill -> systemd."""
    pid = subprocess.run(
        ["systemctl", "show", "courtdraft-backend", "-p", "MainPID", "--value"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not pid.isdigit() or pid == "0":
        log("ОШИБКА: служба не запущена, перезапускать нечего")
        return False
    subprocess.run(["kill", "-9", pid], check=False)
    return wait_health()


def wait_idle(limit_hours: int) -> bool:
    deadline = time.time() + limit_hours * 3600
    while time.time() < deadline:
        if stopped():
            return False
        left = active_jobs()
        if left == 0:
            log("очередь пуста")
            return True
        log(f"в работе/очереди: {left}, ждём")
        time.sleep(120)
    return False


def run_case(case_id: str) -> dict:
    number = case_number(case_id)
    started = time.time()
    log(f"--- дело {number}: запуск проекта на 7B")
    try:
        job = http("POST", f"/cases/{case_id}/draft", {"actor": "system"})
    except Exception as exc:  # noqa: BLE001
        log(f"    не удалось поставить задание: {exc}")
        return {"case": number, "state": "ERROR", "error": str(exc)}

    job_id = job.get("job_id", "")
    deadline = time.time() + CASE_TIMEOUT_MIN * 60
    state = "TIMEOUT"
    error = ""
    while time.time() < deadline:
        if stopped():
            log("    остановка по флагу STOP_7B")
            return {"case": number, "state": "STOPPED"}
        time.sleep(30)
        try:
            info = http("GET", f"/jobs/{job_id}")
        except Exception as exc:  # noqa: BLE001
            log(f"    опрос не удался: {exc}")
            continue
        state = info.get("state", "")
        if state in ("DONE", "FAILED"):
            error = info.get("error", "") or ""
            break

    minutes = round((time.time() - started) / 60, 1)
    log(f"    итог: {state}, {minutes} мин, {error[:120]}")
    return {
        "case": number,
        "case_id": case_id,
        "state": state,
        "minutes": minutes,
        "error": error[:300],
    }


def main() -> None:
    if stopped():
        log("найден STOP_7B — выходим сразу")
        return

    cases = pending_cases()
    if not cases and os.path.exists(CASES_FILE):
        with open(CASES_FILE, encoding="utf-8") as handle:
            cases = [line.strip() for line in handle if line.strip()]
        log(f"очередь пуста, берём сохранённый список: {len(cases)} дел")

    cases = cases[:MAX_CASES]
    log(f"на 7B пойдут {len(cases)} дел (лимит {MAX_CASES})")
    if not cases:
        log("дел для сравнения нет — выходим")
        return

    log("ждём, пока очередь опустеет")
    if not wait_idle(WAIT_IDLE_LIMIT_H):
        log("очередь не опустела за отведённое время — выходим без переключения")
        return

    already = _env_value("MODEL_ROUTING_ENABLED") == "false" and (
        _env_value("OLLAMA_MODEL") == "qwen2.5:7b"
    )
    if already:
        log("стенд уже на 7B — перезапуск не нужен")
    else:
        log("включаем 7B: MODEL_ROUTING_ENABLED=false, OLLAMA_MODEL=qwen2.5:7b")
        set_env("MODEL_ROUTING_ENABLED", "false")
        set_env("OLLAMA_MODEL", "qwen2.5:7b")
        if not restart_backend():
            log("ОШИБКА: служба не поднялась после переключения — возвращаем настройки")
            set_env("MODEL_ROUTING_ENABLED", "true")
            restart_backend()
            return

    results = []
    failures = 0
    for case_id in cases:
        if stopped():
            log("остановка по флагу STOP_7B")
            break
        result = run_case(case_id)
        results.append(result)
        failures = failures + 1 if result["state"] != "DONE" else 0
        if failures >= MAX_CONSECUTIVE_FAILURES:
            log(f"{failures} провала подряд — останавливаемся")
            break

    log("возвращаем быстрый режим: MODEL_ROUTING_ENABLED=true")
    set_env("MODEL_ROUTING_ENABLED", "true")
    restart_backend()

    done = sum(1 for item in results if item["state"] == "DONE")
    avg = (
        round(sum(item["minutes"] for item in results if item["state"] == "DONE") / done, 1)
        if done
        else 0
    )
    with open(REPORT, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "cases": len(cases),
                "done": done,
                "avg_minutes": avg,
                "results": results,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    log(f"готово: {done} из {len(cases)}, среднее {avg} мин. Отчёт: {REPORT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"СБОЙ СКРИПТА: {type(exc).__name__}: {exc}")
        sys.exit(1)
