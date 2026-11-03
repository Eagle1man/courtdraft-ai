"""Выкладка изоляции по коду участника на тестовую ВМ.

Три шага, каждый обязателен:

1. залить изменённые файлы (код + миграция);
2. применить миграцию — колонки `cases.student_code` в базе ВМ нет,
   а приложение при рассинхроне схемы только пишет ошибку в лог и
   продолжает работать, то есть запросы дел упадут уже после выкладки;
3. перезапустить службу и проверить, что фильтр действительно режет.

Пароль берётся из `COURTDRAFT_VM_PASSWORD` (см. vm_credentials.py):
в репозитории его нет и быть не должно.

    $env:COURTDRAFT_VM_PASSWORD='...'
    python scripts\\vm_deploy_isolation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import paramiko

from vm_credentials import vm_credentials

ROOT = Path(__file__).resolve().parent.parent
HOST, USER, PASSWORD = vm_credentials()
REMOTE_ROOT = f"/home/{USER}/courtdraft-ai"

FILES = [
    "backend/app/models/entities.py",
    "backend/app/db/repositories.py",
    "backend/app/services/case_service.py",
    "backend/app/api/routes/cases.py",
    "backend/app/db/bootstrap.py",
    "backend/alembic/versions/20261103_1200_9b3e64d1c7aa_case_student_code.py",
    "frontend/app.js",
]


def run(client: paramiko.SSHClient, command: str, *, check: bool = True) -> str:
    print(f"$ {command}")
    _, stdout, stderr = client.exec_command(command, timeout=180)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    if out:
        print(out)
    if err:
        print(f"[stderr] {err}")
    if check and code != 0:
        raise SystemExit(f"команда завершилась с кодом {code}: {command}")
    return out


def main() -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )

    try:
        sftp = client.open_sftp()
        for rel in FILES:
            remote = f"{REMOTE_ROOT}/{rel}"
            sftp.put(str(ROOT / rel), remote)
            print(f"put {rel}")
        sftp.close()

        backend = f"{REMOTE_ROOT}/backend"
        # stamp ставится на текущую голову, чтобы upgrade применил только
        # новую миграцию: база ВМ создавалась до Alembic и отметки не имеет.
        run(client, f"cd {backend} && .venv/bin/python -m alembic stamp 4a19f2c7e8b1")
        run(client, f"cd {backend} && .venv/bin/python -m alembic upgrade head")
        run(client, "sudo systemctl restart courtdraft-backend")
        run(client, "sleep 12 && systemctl is-active courtdraft-backend")
        run(client, "curl -s -m 20 http://127.0.0.1:8081/api/health")
    finally:
        client.close()


if __name__ == "__main__":
    main()
    sys.exit(0)
