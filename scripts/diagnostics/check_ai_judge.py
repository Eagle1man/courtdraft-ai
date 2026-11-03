"""Проверка полного цикла «ИИ судьи» на стенде.

Создаёт временное дело, загружает материал, запускает формирование проекта,
ждёт завершения и проверяет, что проект не пустой. Дело удаляется в конце.

Запуск:
    python scripts\\diagnostics\\check_ai_judge.py
    set COURTDRAFT_URL=http://127.0.0.1:8081 && python ...
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
case_id = None

TEXT = """ИСКОВОЕ ЗАЯВЛЕНИЕ

В Арбитражный суд города Москвы

Истец: ООО «СтройИнвест», ИНН 7701234567
Ответчик: ООО «РемСтройСервис», ИНН 7712345678

15 января 2026 года между сторонами заключён договор подряда № 12/01-2026
на выполнение отделочных работ. Цена работ — 1 500 000 рублей.

16 января 2026 года истец перечислил ответчику аванс 1 500 000 рублей
платёжным поручением № 87.

25 марта 2026 года стороны подписали акт о приёмке выполненных работ
на сумму 1 000 000 рублей.

Неотработанный аванс составляет 500 000 рублей. Претензия от 26 апреля
2026 года оставлена без удовлетворения.

Прошу взыскать с ООО «РемСтройСервис» неотработанный аванс 500 000 рублей
и неустойку 112 000 рублей.
"""


def main() -> None:
    global case_id
    try:
        created = requests.post(
            f"{API}/api/cases",
            json={
                "case_number": f"AI-{stamp}",
                "court_name": "Арбитражный суд города Москвы",
                "case_type": "civil",
                "is_synthetic": False,
                "student_code": "S01",
            },
            timeout=30,
        )
        created.raise_for_status()
        case_id = created.json()["id"]
        print("дело создано:", created.json()["case_number"])

        upload = requests.post(
            f"{API}/api/cases/{case_id}/documents/upload",
            params={"student_code": "S01"},
            files={"file": ("isk.txt", io.BytesIO(TEXT.encode()), "text/plain")},
            timeout=120,
        )
        print("материал загружен:", upload.status_code)
        upload.raise_for_status()

        started = requests.post(
            f"{API}/api/cases/{case_id}/draft",
            json={"actor": "judge"},
            timeout=60,
        )
        print("задание на проект:", started.status_code)
        started.raise_for_status()
        job_id = started.json()["job_id"]

        state = ""
        for _ in range(60):
            time.sleep(5)
            job = requests.get(f"{API}/api/jobs/{job_id}", timeout=30).json()
            state = job.get("state", "")
            if state in {"DONE", "FAILED"}:
                break
            print(f"  ... {state}")

        print("итог задания:", state)
        if state == "FAILED":
            print("ошибка:", job.get("error"))
            raise SystemExit(1)

        draft = requests.get(f"{API}/api/cases/{case_id}/draft", timeout=30)
        print("проект доступен:", draft.status_code)
        body = draft.json()
        text = body.get("text") or ""
        print("версия:", body.get("version"), "· знаков:", len(text))
        print("проверка:", body.get("verification", {}).get("status")
              if isinstance(body.get("verification"), dict) else body.get("verification"))
        print("первые строки проекта:")
        for line in text.splitlines()[:8]:
            if line.strip():
                print("   ", line[:110])
        assert text.strip(), "проект пустой"
        print("OK: ИИ-судья формирует проект")
    finally:
        if case_id:
            removed = requests.delete(f"{API}/api/cases/{case_id}", timeout=30)
            print("временное дело удалено:", removed.status_code)


if __name__ == "__main__":
    main()
