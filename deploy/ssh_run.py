"""Выполнение команд на ВМ по SSH через paramiko.

Использование:
    python ssh_run.py "команда"
    python ssh_run.py --file script.sh

Параметры подключения берутся из окружения, а не из файла: пароль в
репозитории — это утечка, которая переживает смену пароля и попадает в
историю git. Значения задаются перед запуском:

    $env:DEPLOY_HOST     = "10.19.20.20"
    $env:DEPLOY_USER     = "ubuntuii"
    $env:DEPLOY_PASSWORD = "..."

Пароль можно положить в `.env` проекта (он вне git) — файл читается,
если переменная не задана в окружении.
"""
import os
import sys
from pathlib import Path

import paramiko

PORT = int(os.environ.get("DEPLOY_PORT", "22"))


def _env_file_value(name: str) -> str:
    """Значение из .env проекта: параметр не обязан жить в окружении."""
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def setting(name: str) -> str:
    value = os.environ.get(name) or _env_file_value(name)
    if not value:
        print(
            f"Не задано {name}. Экспортируйте DEPLOY_HOST, DEPLOY_USER, "
            "DEPLOY_PASSWORD или положите их в .env.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        setting("DEPLOY_HOST"),
        port=PORT,
        username=setting("DEPLOY_USER"),
        password=setting("DEPLOY_PASSWORD"),
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
    )
    return client


def run(client: paramiko.SSHClient, command: str, timeout: int = 300):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ssh_run.py <command> | --file <script>")
        return 2

    client = connect()
    try:
        if sys.argv[1] == "--file":
            script = open(sys.argv[2], "r", encoding="utf-8").read()
            code, out, err = run(client, f"bash -s <<'EOF'\n{script}\nEOF")
        else:
            command = " ".join(sys.argv[1:])
            code, out, err = run(client, command)
    finally:
        client.close()

    if out:
        print(out)
    if err:
        print("STDERR:", err, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
