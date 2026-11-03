"""Выкладка изоляции по коду участника на тестовую ВМ.

Зачем такой порядок и почему без sudo:

* `sudo` на стенде просит пароль, а `systemctl` без прав root не работает
  (polkit отвечает «требуется аутентификация»). Поэтому служба не
  останавливается командой: приложение работает под `ubuntuii`, и после
  заливки кода процесс перезапускается сигналом — systemd поднимает его
  сам (`Restart=on-failure`, 5 с).
* Перед перезапуском код проверяется импортом: если новая версия не
  собирается, стенд остаётся на старом коде, а не падает в цикле
  перезапусков (после 5 неудач systemd сдаётся, и поднять службу можно
  только через sudo — то есть руками).
* Схема БД: колонки `cases.student_code` на стенде нет. Приложение при
  рассинхроне схемы только пишет предупреждение в лог и продолжает
  работать, поэтому без миграции запросы дел упадут уже после выкладки.
  `alembic_version` на стенде отсутствует — значит схема соответствует
  ревизии `4a19f2c7e8b1` (проверено: `sections_checked` есть, `student_code`
  нет), её и ставим штампом.
* Пустой `student_code` означает «дело видно всем» (так открывается
  демонстрационный стенд). Оставить 47 прежних дел пустыми — сохранить
  утечку, поэтому коды восстанавливаются из журнала аудита, где они
  писались при создании дела.

Запуск (нужен доступ по ключу `~/.ssh/id_ed25519_lecture`):

    python scripts\\vm_deploy_isolation.py --dry-run
    python scripts\\vm_deploy_isolation.py

Флаги:
    --dry-run     только показать план, ничего не менять
    --no-restart  залить код и мигрировать, но не перезапускать службу
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

import paramiko

# Консоль Windows не знает часть символов (стрелки, «·»): без этого печать
# падает посреди выкладки, и шаг остаётся невыполненным.
sys.stdout.reconfigure(errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST = "10.19.20.20"
USER = "ubuntuii"
REMOTE_ROOT = f"/home/{USER}/courtdraft-ai"
REMOTE_BACKEND = f"{REMOTE_ROOT}/backend"
DB_PATH = f"{REMOTE_ROOT}/data/cases/courtdraft.db"
KEY_PATH = pathlib.Path.home() / ".ssh" / "id_ed25519_lecture"

# Ревизия, которой соответствует схема стенда: `sections_checked` в
# verifications уже есть, `student_code` ещё нет. Проверено запросом.
STAMP_REVISION = "4a19f2c7e8b1"

FILES = [
    "backend/app/models/entities.py",
    "backend/app/db/repositories.py",
    "backend/app/services/case_service.py",
    "backend/app/api/routes/cases.py",
    "backend/app/db/bootstrap.py",
    "backend/alembic/versions/20261103_1200_9b3e64d1c7aa_case_student_code.py",
    "backend/alembic/versions/20261103_1300_c71d5aa40e63_case_is_archived.py",
    "backend/scripts/backfill_student_codes.py",
    "frontend/app.js",
]

# Резервная копия живой базы: sqlite3 backup API снимает согласованный
# снимок, не останавливая приложение.
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

# Состояние схемы: есть ли таблица версий и колонка.
STATE_PY = """
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
cols = [r[1] for r in con.execute("pragma table_info(cases)")]
print("version_table=%s" % ("alembic_version" in tables))
print("student_code=%s" % ("student_code" in cols))
if "student_code" in cols:
    print("cases=%s" % con.execute("select count(*) from cases").fetchone()[0])
    print("with_code=%s" % con.execute(
        "select count(*) from cases where coalesce(student_code,'') != ''").fetchone()[0])
"""

# Проверка изоляции по живому API. Печатает только ASCII, чтобы вывод не
# зависел от кодировки консоли.
VERIFY_PY = """
import json, sqlite3, urllib.request, urllib.error, sys

db, base = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

def count(where):
    return con.execute("select count(*) from cases where " + where).fetchone()[0]

archived = count("is_archived = 1")
visible = count("is_archived = 0")
print("db_archived=%s db_visible=%s" % (archived, visible))

def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None

ok = True
status_all, data_all = get("/api/cases")
total_all = data_all.get("total") if data_all else None
print("api_total=%s (http %s)" % (total_all, status_all))
if status_all != 200 or total_all != visible:
    print("FAIL: list shows a different number of cases than db_visible")
    ok = False
else:
    print("PASS: list matches visible cases, hidden ones are not shown")

status_none, data_none = get("/api/cases?student_code=__nobody__")
total_none = data_none.get("total") if data_none else None
print("nobody_total=%s (http %s)" % (total_none, status_none))
if status_none != 200 or total_none != 0:
    print("FAIL: unknown code still sees cases")
    ok = False
else:
    print("PASS: unknown code sees nothing")

rows = list(con.execute(
    "select id, student_code from cases "
    "where is_archived = 0 and coalesce(student_code,'') != '' limit 1"))
if rows:
    case_id, code = rows[0]
    st_own, _ = get("/api/cases/%s?student_code=%s" % (case_id, code))
    st_foreign, _ = get("/api/cases/%s?student_code=__nobody__" % case_id)
    print("own_case_http=%s foreign_case_http=%s" % (st_own, st_foreign))
    if st_own != 200 or st_foreign != 403:
        print("FAIL: direct access is not isolated")
        ok = False
    else:
        print("PASS: own 200, foreign 403")
else:
    print("SKIP: no visible case with student_code yet (bench is empty by design)")

print("RESULT=%s" % ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
"""


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    params: dict[str, object] = {
        "timeout": 20,
        "look_for_keys": False,
        "allow_agent": False,
    }
    if KEY_PATH.exists():
        params["pkey"] = paramiko.Ed25519Key.from_private_key_file(str(KEY_PATH))
    else:
        import os

        password = os.environ.get("COURTDRAFT_VM_PASSWORD", "")
        if not password:
            raise SystemExit(
                "Нет доступа к ВМ: не найден ключ\n"
                f"  {KEY_PATH}\n"
                "и не задан COURTDRAFT_VM_PASSWORD."
            )
        params["password"] = password

    client.connect(HOST, username=USER, **params)  # type: ignore[arg-type]
    return client


def attempt(client: paramiko.SSHClient, command: str) -> tuple[int, str]:
    """Выполнить команду и вернуть код возврата вместе с выводом."""
    print(f"$ {command}")
    _, stdout, stderr = client.exec_command(command, timeout=300)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    if out:
        print(out)
    if err:
        print(f"[stderr] {err}")
    return code, f"{out}\n{err}"


def run(client: paramiko.SSHClient, command: str, *, check: bool = True) -> str:
    code, text = attempt(client, command)
    if check and code != 0:
        raise SystemExit(f"код {code}: {command}")
    return text.strip()


def put_script(sftp: paramiko.SFTPClient, name: str, body: str) -> str:
    path = f"/tmp/{name}"
    with sftp.open(path, "w") as handle:
        handle.write(body)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="только показать план")
    parser.add_argument(
        "--no-restart", action="store_true", help="не перезапускать службу"
    )
    args = parser.parse_args()

    if args.dry_run:
        print("Файлы к выкладке:")
        for rel in FILES:
            local = ROOT / rel
            print(f"  {'OK ' if local.exists() else 'НЕТ'} {rel}")
        print(f"\nstamp: {STAMP_REVISION} (если таблицы alembic_version нет)")
        print(f"база:  {DB_PATH}")
        print("порядок: backup → upload → import-check → migrate → backfill → restart → verify")
        return

    missing = [rel for rel in FILES if not (ROOT / rel).exists()]
    if missing:
        raise SystemExit("Нет локальных файлов:\n  " + "\n  ".join(missing))

    client = connect()
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{REMOTE_ROOT}/data/cases/backups/courtdraft-{stamp}.db"
        files_backup = f"{REMOTE_ROOT}/data/cases/backups/files-{stamp}"

        sftp = client.open_sftp()
        run(client, f"mkdir -p {REMOTE_ROOT}/data/cases/backups {files_backup}")

        state_path = put_script(sftp, "_state.py", STATE_PY)
        backup_py = put_script(sftp, "_backup.py", BACKUP_PY)

        state = run(
            client,
            f"cd {REMOTE_BACKEND} && .venv/bin/python {state_path} {DB_PATH}",
        )
        has_version = "version_table=True" in state
        has_column = "student_code=True" in state

        pid = run(
            client,
            "systemctl show courtdraft-backend -p MainPID --value",
        ).strip()
        if not pid.isdigit() or pid == "0":
            raise SystemExit(
                "Служба не запущена (MainPID=0). Поднять её можно только\n"
                "через sudo — сделайте это руками: sudo systemctl start courtdraft-backend"
            )

        print(f"служба работает, pid={pid}, версия схемы: "
              f"{'есть' if has_version else 'нет'}, колонка: "
              f"{'есть' if has_column else 'нет'}")

        # 1. Резервная копия базы до любых изменений.
        run(
            client,
            f"cd {REMOTE_BACKEND} && .venv/bin/python {backup_py} {DB_PATH} {backup}",
        )

        # 2. Копии заменяемых файлов — чтобы откатить одной командой.
        #    Новых файлов на стенде ещё нет, поэтому отсутствие — не ошибка.
        for rel in FILES:
            src = f"{REMOTE_ROOT}/{rel}"
            dst = f"{files_backup}/{rel.replace('/', '_')}"
            run(client, f"if [ -f {src} ]; then cp -a {src} {dst}; else echo 'новый файл: {rel}'; fi")

        # 3. Код.
        for rel in FILES:
            sftp.put(str(ROOT / rel), f"{REMOTE_ROOT}/{rel}")
            print(f"put {rel}")
        sftp.close()

        # 4. Проверка импорта ДО перезапуска: сломанный код не должен
        #    сменить работающий процесс.
        run(client, f"cd {REMOTE_BACKEND} && .venv/bin/python -c 'import app.main'")

        # 5. Миграция.
        if has_column:
            print("колонка уже есть — stamp/upgrade пропущены")
        else:
            if not has_version:
                run(
                    client,
                    f"cd {REMOTE_BACKEND} && .venv/bin/python -m alembic stamp {STAMP_REVISION}",
                )
            else:
                print("таблица версий уже есть — stamp пропущен")
            # База занята работающим приложением: ALTER берёт блокировку
            # записи, поэтому несколько повторов на случай 'database is locked'.
            for attempt_no in range(1, 4):
                code, text = attempt(
                    client,
                    f"cd {REMOTE_BACKEND} && .venv/bin/python -m alembic upgrade head",
                )
                if code == 0:
                    break
                if "database is locked" not in text.lower():
                    raise SystemExit("миграция не применилась — см. вывод выше")
                print(f"база занята, повтор {attempt_no}/3")
                time.sleep(3)
            else:
                raise SystemExit(
                    "миграция не смогла взять блокировку базы за 3 попытки.\n"
                    "Файлы стенда уже заменены, база не изменена. Вернуть файлы:\n"
                    f"  {files_backup}"
                )

        # 6. Коды участника из журнала.
        run(client, f"cd {REMOTE_BACKEND} && .venv/bin/python -m scripts.backfill_student_codes")

        # 7. Перезапуск: SIGKILL — единственный сигнал, после которого
        #    systemd поднимает службу сам (SIGTERM считается чистым выходом).
        if args.no_restart:
            print("перезапуск пропущен (--no-restart): новый код ещё не в работе")
        else:
            kill_code, _ = attempt(client, f"kill -9 {pid}")
            if kill_code != 0:
                raise SystemExit(
                    f"не удалось перезапустить процесс {pid}: служба осталась на старом коде"
                )
            healthy = False
            for _ in range(24):
                time.sleep(5)
                out = run(
                    client,
                    "curl -s -m 5 http://127.0.0.1:8081/api/health",
                    check=False,
                )
                if '"status"' in out or '"ok"' in out.lower():
                    healthy = True
                    break
            if not healthy:
                run(client, "journalctl -u courtdraft-backend -n 40 --no-pager", check=False)
                raise SystemExit(
                    "Служба не поднялась после перезапуска. Файлы стенда:\n"
                    f"  {files_backup}\n"
                    "Вернуть прежние файлы, затем перезапустить вручную."
                )
            print("служба поднялась")

        # 8. Проверка изоляции по живому API.
        sftp2 = client.open_sftp()
        verify = put_script(sftp2, "_verify.py", VERIFY_PY)
        sftp2.close()
        run(
            client,
            f"{REMOTE_BACKEND}/.venv/bin/python {verify} {DB_PATH} http://127.0.0.1:8081",
            check=False,
        )

        print(f"\nрезервная копия базы: {backup}")
        print(f"копии заменённых файлов: {files_backup}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
    sys.exit(0)
