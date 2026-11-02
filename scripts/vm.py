"""Мини-клиент SSH к тестовой ВМ (только для локальной отладки)."""

from __future__ import annotations

import argparse
import sys

import paramiko

from vm_credentials import vm_credentials

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HOST, USER, PASSWORD = vm_credentials()


def run(command: str, timeout: int = 300) -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    code = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    if err:
        sys.stdout.write("\n[stderr]\n" + err)
    client.close()
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    return run(args.command, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
