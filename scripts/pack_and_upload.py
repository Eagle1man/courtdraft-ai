"""Упаковка исходников проекта и загрузка на тестовую ВМ."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import paramiko

from vm_credentials import vm_credentials

ROOT = Path(__file__).resolve().parent.parent
HOST, USER, PASSWORD = vm_credentials()
REMOTE_DIR = f"/home/{USER}/courtdraft-ai"
ARCHIVE = Path(os.environ.get("TEMP", r"C:\Users\diorl\AppData\Local\Temp")) / "courtdraft.tar.gz"

EXCLUDE_TOP = {".git", ".venv", "node_modules", "models", "artifacts"}


def collect() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.splitlines()
    paths = [ROOT / p for p in out if p.strip()]
    extra = [ROOT / ".env", ROOT / "data/cases/courtdraft.db"]
    extra += list((ROOT / "data/cases/originals/72a3ed02-99cc-4cb2-84b7-d31f8cded105").glob("*"))
    paths.extend(p for p in extra if p.exists())
    return [
        p
        for p in paths
        if p.is_file()
        and p.relative_to(ROOT).parts[0] not in EXCLUDE_TOP
        and "__pycache__" not in p.relative_to(ROOT).parts
    ]


def make_archive(files: list[Path]) -> None:
    if ARCHIVE.exists():
        ARCHIVE.unlink()
    with tarfile.open(ARCHIVE, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in files:
            tar.add(path, arcname=str(path.relative_to(ROOT)).replace("\\", "/"))
    print(f"archive: {ARCHIVE} ({ARCHIVE.stat().st_size / 1024 / 1024:.1f} MB, {len(files)} files)")


def upload() -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASSWORD, timeout=20, look_for_keys=False, allow_agent=False)
    sftp = client.open_sftp()
    sftp.put(str(ARCHIVE), f"/home/{USER}/courtdraft.tar.gz")
    sftp.close()
    client.close()
    print("uploaded")


if __name__ == "__main__":
    files = collect()
    make_archive(files)
    if "--no-upload" not in sys.argv:
        upload()
