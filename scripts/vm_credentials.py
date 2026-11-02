"""Параметры доступа к тестовой ВМ из окружения.

Пароль не хранится в репозитории: перед запуском скриптов задайте
`COURTDRAFT_VM_PASSWORD` (и при необходимости HOST/USER).
"""

from __future__ import annotations

import os


def vm_credentials() -> tuple[str, str, str]:
    host = os.environ.get("COURTDRAFT_VM_HOST", "10.19.20.20")
    user = os.environ.get("COURTDRAFT_VM_USER", "ubuntuii")
    password = os.environ.get("COURTDRAFT_VM_PASSWORD", "")
    if not password:
        raise SystemExit(
            "Не задан COURTDRAFT_VM_PASSWORD. "
            "Пример: $env:COURTDRAFT_VM_PASSWORD='...' (PowerShell)."
        )
    return host, user, password
