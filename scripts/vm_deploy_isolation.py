"""Выкладка изоляции по коду участника на тестовую ВМ.

Порядок жёсткий, и каждый шаг закрывает конкретную поломку:

1. остановить службу — иначе ALTER TABLE упрётся в занятую базу
   (`database is locked`) и миграция упадёт на середине;
2. снять резервную копию — в базе 47 дел стенда, откатывать нечем;
3. залить код и миграцию;
4. применить миграцию: колонки `cases.student_code` в базе ВМ нет, а
   приложение при рассинхроне схемы только пишет ошибку в лог и работает
   дальше, то есть запросы дел упадут уже после выкладки;
5. восстановить коды участника из журнала аудита — иначе все прежние
   дела останутся «анонимными», а анонимные видны всем: утечка осталась
   бы ровно там, где её и убираем;
6. поднять службу и проверить, что фильтр действительно режет.

Доступ: ключ `~/.ssh/id_ed25519_lecture` либо `COURTDRAFT_VM_PASSWORD`.

    python scripts\\vm_deploy_isolation.py --dry-run
    python scripts\\vm_deploy_isolation.py
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

import paramiko

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
    "backend/scripts/backfill_student_codes.py",
    "frontend/app.js",
]

BACKUP_PY = """
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
out = sqlite3.connect(dst)
with out:
    con.backup(out)
out.close()
con.close()
print('backup ok:', dst)
"""

HAS_VERSION_PY = """
import sqlite3
con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
if 'alembic_version' in tables:
    print('yes', [r[0] for r in con.execute('select * from alembic_version')])
else:
    print('no')
""".replace("{DB}", DB_PATH)


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


def run(client: paramiko.SSHClient, command: str, *, check: bool = True) -> str:
    print(f"$ {command}")
    _, stdout, stderr = client.exec_command(command, timeout=300)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    if out:
        print(out)
    if err:
        print(f"[stderr] {err}")
    if check and code != 0:
        raise SystemExit(f"код {code}: {command}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="только показать план")
    args = parser.parse_args()

    if args.dry_run:
        print("Файлы к выкладке:")
        for rel in FILES:
            local = ROOT / rel
            print(f"  {'OK ' if local.exists() else 'НЕТ'} {rel}")
        print(f"\nstamp: {STAMP_REVISION} (если таблицы alembic_version нет)")
        print(f"база:  {DB_PATH}")
        print("порядок: stop → backup → upload → migrate → backfill → start")
        return

    client = connect()
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{REMOTE_ROOT}/data/cases/backups/courtdraft-{stamp}.db"

        sftp = client.open_sftp()
        run(client, f"mkdir -p {REMOTE_ROOT}/data/cases/backups")

        # 1. Служба останавливается: иначе ALTER TABLE не возьмёт блокировку.
        run(client, "sudo systemctl stop courtdraft-backend")

        # 2. Резервная копия до любых изменений.
        with sftp.open("/tmp/_backup_db.py", "w") as handle:
            handle.write(BACKUP_PY)
        run(
            client,
            f"cd {REMOTE_BACKEND} && .venv/bin/python /tmp/_backup_db.py {DB_PATH} {backup}",
        )

        # 3. Код.
        for rel in FILES:
            sftp.put(str(ROOT / rel), f"{REMOTE_ROOT}/{rel}")
            print(f"put {rel}")
        sftp.close()

        # 4. Миграция. stamp ставится один раз: если таблица версий уже есть,
        # значит миграции тут применялись и stamp перезаписал бы историю.
        with sftp.open("/tmp/_has_version.py", "w") as handle:
            handle.write(HAS_VERSION_PY)
        sftp.close()

        version = run(client, f"cd {REMOTE_BACKEND} && .venv/bin/python /tmp/_has_version.py")
        if version.startswith("no"):
            run(client, f"cd {REMOTE_BACKEND} && .venv/bin/python -m alembic stamp {STAMP_REVISION}")
        else:
            print(f"таблица версий уже есть ({version}) — stamp пропущен")

        run(client, f"cd {REMOTE_BACKEND} && .venv/bin/python -m alembic upgrade head")

        # 5. Коды участника из журнала.
        run(client, f"cd {REMOTE_BACKEND} && .venv/bin/python -m scripts.backfill_student_codes")

        # 6. Подъём и проверка.
        run(client, "sudo systemctl start courtdraft-backend")
        run(client, "sleep 12 && systemctl is-active courtdraft-backend")
        run(client, "curl -s -m 20 http://127.0.0.1:8081/api/health | head -c 300")

        print(f"\nрезервная копия: {backup}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
    sys.exit(0)
