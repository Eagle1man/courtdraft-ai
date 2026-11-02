"""Смоук-тест квоты: дело -> проект -> отзыв -> разбивка по модели."""

from __future__ import annotations

import time
import uuid

import httpx

BASE = "http://127.0.0.1:8081"
TEXT = (
    "ИСКОВОЕ ЗАЯВЛЕНИЕ\n\n"
    "Истец: Смирнова Ольга Викторовна.\n"
    "Ответчик: Кузнецов Андрей Петрович.\n\n"
    "Прошу взыскать задолженность по договору займа в размере 150 000 рублей, "
    "проценты за пользование чужими денежными средствами в размере 12 340 рублей "
    "и расходы по уплате государственной пошлины в размере 4 200 рублей.\n\n"
    "В обоснование указано, что между сторонами заключён договор займа, "
    "денежные средства переданы по расписке, однако в установленный срок "
    "не возвращены. Ответчик иск не признал, ссылаясь на частичный возврат.\n"
)


def main() -> int:
    code = f"SMOKE-{uuid.uuid4().hex[:6]}"
    with httpx.Client(base_url=BASE, timeout=900.0) as client:
        created = client.post(
            "/api/cases",
            json={
                "case_number": code,
                "court_name": "Тестовый суд",
                "case_type": "civil",
                "is_synthetic": True,
                "student_code": code,
            },
        )
        created.raise_for_status()
        case_id = created.json()["id"]
        print("case:", case_id)

        uploaded = client.post(
            f"/api/cases/{case_id}/documents/upload",
            files={"file": ("claim.txt", TEXT.encode("utf-8"), "text/plain")},
        )
        uploaded.raise_for_status()
        print("document:", uploaded.json()["characters"], "chars")

        job = client.post(f"/api/cases/{case_id}/draft", json={"actor": "judge"})
        job.raise_for_status()
        job_id = job.json()["job_id"]
        print("job:", job_id)

        state = ""
        for _ in range(180):
            time.sleep(5)
            data = client.get(f"/api/jobs/{job_id}").json()
            state = str(data.get("state"))
            if state in {"DONE", "FAILED"}:
                print("job state:", state, "error:", data.get("error"))
                break
        if state != "DONE":
            return 1

        feedback = client.post(
            "/api/feedback",
            json={
                "case_id": case_id,
                "rating": 5,
                "verdict": "ACCEPTED",
                "comment": "Смоук-тест квоты",
                "author_role": "student",
                "author_name": code,
            },
        )
        feedback.raise_for_status()
        print("feedback model:", feedback.json()["model_name"])

        stats = client.get("/api/feedback/stats").json()
        print("by_model:", stats.get("by_model"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
