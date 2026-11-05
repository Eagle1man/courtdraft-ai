"""Сводка по стенду во время теста: что сломалось и на чём именно.

Зачем отдельный скрипт: во время теста студентов важно не «посмотреть
логи глазами», а получить одинаковый срез каждый раз — иначе сравнить
два дня теста нечем. Скрипт только читает: база открывается в режиме
read-only, журнал берётся через journalctl, служба опрашивается по
health.

Запуск на стенде:
    .venv/bin/python -m scripts.test_status
    .venv/bin/python -m scripts.test_status --hours 6 --errors 20
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta

sys.stdout.reconfigure(errors="replace")

DEFAULT_DB = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "data"
    / "cases"
    / "courtdraft.db"
)
HEALTH_URL = "http://127.0.0.1:8081/api/health"
SERVICE = "courtdraft-backend"

_ERROR_MARKERS = ("ERROR", "CRITICAL", "Traceback (most recent call last)")
_WARN_MARKERS = ("WARNING",)
_STATUS = re.compile(r'"\s*(?:GET|POST|PUT|PATCH|DELETE)\s+[^"]*"\s+(\d{3})')


def _db_path(value: str) -> pathlib.Path:
    """Путь к базе: аргумент, затем DATABASE_URL, затем путь стенда.

    DATABASE_URL читается из окружения, а не из `.env`: скрипт запускают
    на стенде, где переменная не экспортирована, и тогда остаётся путь
    по умолчанию — от корня проекта, а не от каталога `backend`.
    """
    if value:
        return pathlib.Path(value)
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite"):
        raw = url.split("///")[-1]
        if raw:
            return pathlib.Path(raw)
    return DEFAULT_DB


def _connect(path: pathlib.Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _health() -> None:
    print("## Здоровье службы")
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=10) as response:
            data = json.load(response)
    except Exception as exc:  # noqa: BLE001
        print(f"  НЕ ОТВЕЧАЕТ: {exc}")
        return
    bad = [c for c in data.get("components", []) if c.get("state") not in {"OK", "MOCK"}]
    print(f"  статус: {data.get('status')}")
    for component in bad:
        print(f"  не в порядке: {component.get('name')} — {component.get('state')}")
    for warning in data.get("warnings", []):
        print(f"  предупреждение: {warning}")


def _jobs(connection: sqlite3.Connection, since: str) -> None:
    print("\n## Задания генерации")
    rows = connection.execute(
        "select state, count(*) n from jobs where created_at >= ? group by state",
        (since,),
    ).fetchall()
    if not rows:
        print("  за период заданий не было")
    for row in rows:
        print(f"  {row['state']}: {row['n']}")

    failed = connection.execute(
        """
        select id, case_id, error, attempts, created_at
        from jobs
        where created_at >= ? and state = 'FAILED'
        order by created_at desc
        limit 15
        """,
        (since,),
    ).fetchall()
    for row in failed:
        print(f"  сбой {row['created_at']}: {row['error'][:200]}")


def _drafts(connection: sqlite3.Connection, since: str) -> None:
    print("\n## Проекты и проверки")
    rows = connection.execute(
        """
        select d.status, count(*) n
        from drafts d
        where d.updated_at >= ?
        group by d.status
        """,
        (since,),
    ).fetchall()
    for row in rows:
        print(f"  статус проекта {row['status']}: {row['n']}")

    checks = connection.execute(
        """
        select v.status, count(*) n
        from verifications v
        where v.created_at >= ?
        group by v.status
        """,
        (since,),
    ).fetchall()
    for row in checks:
        print(f"  проверка {row['status']}: {row['n']}")

    per_version = connection.execute(
        """
        select count(*) n, sum(case when author = 'user' then 1 else 0 end) правок
        from draft_versions
        where created_at >= ?
        """,
        (since,),
    ).fetchone()
    saved = per_version["n"]
    human = per_version["правок"]
    print(f"  версий сохранено: {saved}, из них правок человека: {human}")


def _cases(connection: sqlite3.Connection, since: str) -> None:
    print("\n## Дела")
    row = connection.execute(
        "select count(*) n from cases where created_at >= ?", (since,)
    ).fetchone()
    archived = connection.execute(
        "select count(*) n from cases where is_archived = 1"
    ).fetchone()
    print(f"  создано за период: {row['n']}")
    print(f"  скрыто как legacy (всего): {archived['n']}")


def _journal(hours: int, errors: int) -> None:
    print(f"\n## Журнал службы за {hours} ч")
    try:
        result = subprocess.run(  # noqa: S603 - команда собрана здесь, внешних данных в ней нет
            [
                "/usr/bin/journalctl",  # noqa: S607 - полный путь: служба вызывает без PATH
                "-u",
                SERVICE,
                "--since",
                f"-{hours}h",
                "--no-pager",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  журнал недоступен: {exc}")
        return

    lines = result.stdout.splitlines()
    codes: Counter[str] = Counter()
    for line in lines:
        match = _STATUS.search(line)
        if match:
            codes[match.group(1)] += 1
    errors_lines = [line for line in lines if any(m in line for m in _ERROR_MARKERS)]
    warnings = [line for line in lines if any(m in line for m in _WARN_MARKERS)]

    bad = {code: n for code, n in codes.items() if code.startswith("5")}
    print(f"  ответов 5xx: {sum(bad.values())} {bad or ''}")
    print(f"  ответов 4xx: {sum(n for c, n in codes.items() if c.startswith('4'))}")
    print(f"  строк с ERROR/Traceback: {len(errors_lines)}")
    print(f"  строк с WARNING: {len(warnings)}")
    for line in errors_lines[-errors:]:
        print("   !", line[:240])
    for line in warnings[-5:]:
        print("   ~", line[:200])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--errors", type=int, default=10)
    args = parser.parse_args()

    since = (datetime.now(UTC) - timedelta(hours=args.hours)).strftime("%Y-%m-%d %H:%M:%S")
    path = _db_path(args.db)
    print(f"# Стенд на {datetime.now(UTC).strftime('%d.%m.%Y %H:%M UTC')}, период {args.hours} ч")
    print(f"база: {path}")
    _health()

    if not path.exists():
        print(f"\nбаза не найдена: {path}")
        _journal(args.hours, args.errors)
        return

    connection = _connect(path)
    try:
        _jobs(connection, since)
        _drafts(connection, since)
        _cases(connection, since)
    finally:
        connection.close()

    _journal(args.hours, args.errors)


if __name__ == "__main__":
    main()
