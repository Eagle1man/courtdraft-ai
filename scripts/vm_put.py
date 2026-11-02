"""Загрузка отдельных файлов проекта на тестовую ВМ."""

from __future__ import annotations

import sys
from pathlib import Path

import paramiko

from vm_credentials import vm_credentials

ROOT = Path(__file__).resolve().parent.parent
HOST, USER, PASSWORD = vm_credentials()
REMOTE_ROOT = f"/home/{USER}/courtdraft-ai"


def main(files: list[str]) -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20, look_for_keys=False, allow_agent=False)
    sftp = client.open_sftp()
    for rel in files:
        local = ROOT / rel
        remote = f"{REMOTE_ROOT}/{rel.replace(chr(92), '/')}"
        sftp.put(str(local), remote)
        print(f"put {rel}")
    sftp.close()
    client.close()


if __name__ == "__main__":
    main(sys.argv[1:])
