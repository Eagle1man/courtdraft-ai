"""Проверка изоляции и загрузки материалов по API.

Создаёт дело, грузит файл, проверяет, что свой код видит материал,
чужой получает 403. Дело удаляется в конце.

Запуск:
    python scripts\\diagnostics\\check_isolation.py
"""

from __future__ import annotations

import io
import os
import sys
import time

import requests

sys.stdout.reconfigure(errors="replace")

API = os.environ.get("COURTDRAFT_URL", "http://10.19.20.20:8081").rstrip("/")
stamp = str(int(time.time()))[-6:]
created = []


def main() -> None:
    try:
        case = requests.post(
            f"{API}/api/cases",
            json={
                "case_number": f"UP-{stamp}",
                "court_name": "Проверочный суд",
                "case_type": "civil",
                "is_synthetic": False,
                "student_code": "S01",
            },
            timeout=30,
        )
        print("создание дела:", case.status_code)
        assert case.status_code == 201, case.text
        case_id = case.json()["id"]
        created.append(case_id)

        text = "ИСКОВОЕ ЗАЯВЛЕНИЕ\nВзыскать 150 000 руб.\n"
        upload = requests.post(
            f"{API}/api/cases/{case_id}/documents/upload",
            params={"student_code": "S01"},
            files={"file": ("01_isk.txt", io.BytesIO(text.encode()), "text/plain")},
            timeout=120,
        )
        print("загрузка:", upload.status_code, upload.text[:120])
        assert upload.status_code == 201, upload.text

        docs = requests.get(
            f"{API}/api/cases/{case_id}/documents",
            params={"student_code": "S01"},
            timeout=30,
        ).json()
        print("материалы дела:", [d["filename"] for d in docs])
        assert [d["filename"] for d in docs] == ["01_isk.txt"]

        other = requests.get(
            f"{API}/api/cases/{case_id}/documents",
            params={"student_code": "S02"},
            timeout=30,
        )
        print("чужой код видит материалы:", other.status_code)
        assert other.status_code == 403, other.status_code

        print("OK: загрузка работает, материалы не смешиваются")
    finally:
        for case_id in created:
            code = requests.delete(f"{API}/api/cases/{case_id}", timeout=30)
            print("удалено:", code.status_code)


if __name__ == "__main__":
    main()
