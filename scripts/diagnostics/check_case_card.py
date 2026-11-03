"""Проверка карточки дела на стенде (через localhost на самой ВМ).

Создаёт дело без номера и суда, регистрирует документ с реквизитами в шапке
и проверяет, что карточка заполнилась из материалов. Дело удаляется в конце.

Запуск с машины, где есть SSH-доступ к стенду:
    python scripts\\diagnostics\\check_case_card.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import paramiko

sys.stdout.reconfigure(errors="replace")

HOST = os.environ.get("COURTDRAFT_VM_HOST", "10.19.20.20")
USER = os.environ.get("COURTDRAFT_VM_USER", "ubuntuii")
KEY = pathlib.Path(
    os.environ.get("COURTDRAFT_VM_KEY", str(pathlib.Path.home() / ".ssh" / "id_ed25519_lecture"))
)
LOCAL = pathlib.Path(tempfile.mkdtemp(prefix="courtdraft_card_")) / "card_local.txt"

PY = r'''
import json, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:8081"
stamp = str(int(time.time()))[-6:]

def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return r.status, json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]

STATUS = []

st, created = call("POST", "/api/cases", {
    "case_number": "б/н " + stamp, "court_name": "", "case_type": "civil",
    "is_synthetic": False, "student_code": "S01"})
print("create:", st, created.get("case_number"), repr(created.get("court_name")))
case_id = created["id"]

# Документ регистрируется через JSON-эндпоинт: он на том же пути обновления карточки.
DOC = (
    "ИСКОВОЕ ЗАЯВЛЕНИЕ\n"
    "Мировой судья судебного участка № 123 района Хамовники г. Москвы 119034\n"
    "Дело № 2-456/2025\n"
    "Истец: Смирнова О.В.\n"
)
st, doc = call("POST", "/api/cases/%s/documents" % case_id, {
    "filename": "01_isk.txt", "document_type": "CLAIM", "text": DOC})
print("register:", st)

st, case = call("GET", "/api/cases/%s?student_code=S01" % case_id)
print("after:", repr(case.get("case_number")), "|", repr(case.get("court_name")))
ok = case.get("case_number") == "2-456/2025" and case.get("court_name", "").startswith("Мировой судья")
print("RESULT:", "PASS" if ok else "FAIL")

print("cleanup:", call("DELETE", "/api/cases/%s" % case_id)[0])
'''


def main() -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        pkey=paramiko.Ed25519Key.from_private_key_file(str(KEY)),
        timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )
    sftp = client.open_sftp()
    with sftp.open("/tmp/_card_local.py", "w") as handle:
        handle.write(PY)
    sftp.close()
    _, stdout, stderr = client.exec_command(
        "cd /home/ubuntuii/courtdraft-ai/backend && .venv/bin/python /tmp/_card_local.py",
        timeout=180,
    )
    code = stdout.channel.recv_exit_status()
    data = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace").strip()
    client.close()
    LOCAL.write_text(data + ("\nstderr: " + err if err else ""), encoding="utf-8")
    print("exit:", code, "->", LOCAL)


if __name__ == "__main__":
    main()
