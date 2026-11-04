"""Выкладка обновления (юрисдикция, даты, неполные проекты, задания в БД).

Чем отличается от `vm_deploy_isolation.py`: тот заливал отдельный список
файлов под конкретную задачу изоляции. Здесь изменений много и они
разбросаны по каталогам, поэтому заливается архив выбранных каталогов
целиком. Список каталогов задан явно — база, данные дел и `.venv` в
архив не попадают.

Порядок:
    база + копии файлов → заливка → проверка импорта → миграции →
    пересчёт вида судопроизводства → перезапуск → проверка живого API

Почему перезапуск сигналом: `sudo` на стенде просит пароль, а служба
работает под `ubuntuii` с `Restart=on-failure`. `kill -9 <MainPID>`
поднимает её через systemd. Импорт проверяется до перезапуска: если
новая версия не собирается, стенд остаётся на старом коде.

Ограничение, о котором надо помнить: перезапуск убивает незавершённые
задания генерации. Выкладывать только при остановленном тесте.

Запуск (нужен ключ `~/.ssh/id_ed25519_lecture`):

    python scripts\\vm_deploy_update.py --dry-run
    python scripts\\vm_deploy_update.py
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys
import tarfile
import time

import paramiko

sys.stdout.reconfigure(errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST = "10.19.20.20"
USER = "ubuntuii"
REMOTE_ROOT = f"/home/{USER}/courtdraft-ai"
REMOTE_BACKEND = f"{REMOTE_ROOT}/backend"
DB_PATH = f"{REMOTE_ROOT}/data/cases/courtdraft.db"
KEY_PATH = pathlib.Path.home() / ".ssh" / "id_ed25519_lecture"

#: Каталоги, которые заливаются целиком.
DIRS = (
    "backend/app",
    "backend/alembic",
    "backend/scripts",
    "data/templates",
    "frontend",
)

#: Что не попадает в архив.
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

BACKUP_PY = """
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
out = sqlite3.connect(dst)
with out:
    con.backup(out)
out.close()
con.close()
print("backup ok")
"""

STATE_PY = """
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
cols = [r[1] for r in con.execute("pragma table_info(cases)")]
print("jobs_table=%s" % ("jobs" in tables))
print("case_type=%s" % ("case_type" in cols))
if "case_type" in cols:
    for row in con.execute("select case_type, count(*) from cases group by case_type"):
        print("type=%s:%s" % (row[0], row[1]))
"""


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507 - стенд в локальной сети, ключ уже известен
    client.connect(
        HOST,
        username=USER,
        pkey=paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH)),
        timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def attempt(client: paramiko.SSHClient, command: str) -> tuple[int, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=1800)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, (out + err).strip()


def run(client: paramiko.SSHClient, command: str, *, check: bool = True) -> str:
    code, text = attempt(client, command)
    if check and code != 0:
        raise SystemExit(f"код {code}: {command}\n{text[-2000:]}")
    return text


def build_archive() -> bytes:
    """Собрать tar.gz из DIRS, исключая кэши и байткод."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in DIRS:
            base = ROOT / rel
            for path in sorted(base.rglob("*")):
                if path.is_dir():
                    continue
                if any(part in EXCLUDE_DIRS for part in path.parts):
                    continue
                if path.suffix in EXCLUDE_SUFFIXES:
                    continue
                tar.add(path, arcname=path.relative_to(ROOT).as_posix())
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="только показать план")
    parser.add_argument(
        "--no-restart", action="store_true", help="не перезапускать службу"
    )
    args = parser.parse_args()

    if args.dry_run:
        print("Каталоги к выкладке:")
        for rel in DIRS:
            base = ROOT / rel
            count = sum(1 for _ in base.rglob("*") if _.is_file()) if base.exists() else 0
            print(f"  {'OK ' if base.exists() else 'НЕТ'} {rel} ({count} файлов)")
        print(f"\nбаза:  {DB_PATH}")
        print(
            "порядок: backup -> upload -> import-check -> alembic upgrade -> "
            "reclassify -> restart -> verify"
        )
        return

    missing = [rel for rel in DIRS if not (ROOT / rel).exists()]
    if missing:
        raise SystemExit("Нет локальных каталогов:\n  " + "\n  ".join(missing))

    client = connect()
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_db = f"{REMOTE_ROOT}/data/cases/backups/courtdraft-{stamp}.db"
        backup_files = f"{REMOTE_ROOT}/data/cases/backups/files-{stamp}.tar.gz"

        run(client, f"mkdir -p {REMOTE_ROOT}/data/cases/backups")

        pid = run(
            client, "systemctl show courtdraft-backend -p MainPID --value"
        ).strip()
        if not pid.isdigit() or pid == "0":
            raise SystemExit(
                "Служба не запущена (MainPID=0). Поднять её можно только\n"
                "через sudo — сделайте это руками: sudo systemctl start courtdraft-backend"
            )
        version = run(
            client, f"cd {REMOTE_BACKEND} && .venv/bin/python -m alembic current 2>&1"
        )
        print(f"служба pid={pid}; версия схемы: {version.splitlines()[-1]}")

        # 1. Резервные копии: база и заменяемые файлы.
        sftp = client.open_sftp()
        with sftp.open("/tmp/_backup.py", "w") as handle:  # noqa: S108 - служебный файл на стенде
            handle.write(BACKUP_PY)
        with sftp.open("/tmp/_state.py", "w") as handle:  # noqa: S108 - служебный файл на стенде
            handle.write(STATE_PY)
        run(
            client,
            f"cd {REMOTE_BACKEND} && .venv/bin/python /tmp/_backup.py "
            f"{DB_PATH} {backup_db}",
        )
        run(
            client,
            f"cd {REMOTE_ROOT} && tar -czf {backup_files} "
            + " ".join(DIRS),
        )
        print(f"бэкап базы:   {backup_db}")
        print(f"бэкап файлов: {backup_files}")

        # 2. Заливка архива.
        payload = build_archive()
        print(f"архив: {len(payload) / 1024:.0f} КБ")
        with sftp.open("/tmp/courtdraft-update.tar.gz", "wb") as handle:  # noqa: S108 - архив для стенда
            handle.write(payload)
        sftp.close()
        run(client, "tar -xzf /tmp/courtdraft-update.tar.gz -C " + REMOTE_ROOT)

        # 3. Проверка импорта до миграций и перезапуска.
        run(
            client,
            f"cd {REMOTE_BACKEND} && .venv/bin/python -c "
            "\"import app.main, app.ingestion.jurisdiction, app.drafting.date_guard\"",
        )
        print("импорт: OK")

        # 4. Миграции.
        run(client, f"cd {REMOTE_BACKEND} && .venv/bin/python -m alembic upgrade head")

        # 5. Вид судопроизводства у прежних дел: сначала показать, потом применить.
        dry = run(
            client,
            f"cd {REMOTE_BACKEND} && .venv/bin/python -m "
            "scripts.reclassify_jurisdiction --dry-run",
        )
        print(dry)
        run(
            client,
            f"cd {REMOTE_BACKEND} && .venv/bin/python -m "
            "scripts.reclassify_jurisdiction",
        )

        state = run(
            client, f"cd {REMOTE_BACKEND} && .venv/bin/python /tmp/_state.py {DB_PATH}"
        )
        print(state)

        if args.no_restart:
            print("перезапуск пропущен (--no-restart): служба работает на старом коде")
            return

        # 6. Перезапуск сигналом: systemd поднимает службу сам.
        run(client, f"kill -9 {pid}", check=False)
        healthy = False
        for _ in range(60):
            time.sleep(3)
            code, text = attempt(
                client, "curl -s -m 5 http://127.0.0.1:8081/api/health"
            )
            if code == 0 and '"status"' in text:
                healthy = True
                break
        if not healthy:
            raise SystemExit(
                "служба не ответила за 3 минуты. Смотрите журнал:\n"
                "  journalctl -u courtdraft-backend -n 100 --no-pager\n"
                f"откат базы: cp {backup_db} {DB_PATH}\n"
                f"откат файлов: tar -xzf {backup_files} -C {REMOTE_ROOT}"
            )

        # 7. Проверки на живом стенде.
        ui = run(
            client,
            "curl -s http://127.0.0.1:8081/app/index.html | "
            "grep -o 'app.js?v=[0-9.]*'",
        )
        tmpl = run(
            client,
            f"test -f {REMOTE_ROOT}/data/templates/arbitration_judgment.json "
            "&& echo arbitration_template=OK",
        )
        print("health: OK")
        print(ui.strip())
        print(tmpl.strip())
        print(
            f"откат: cp {backup_db} {DB_PATH} && "
            f"tar -xzf {backup_files} -C {REMOTE_ROOT}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
