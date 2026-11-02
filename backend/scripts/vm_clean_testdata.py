"""Убрать артефакты проверки: тестовые дела, тестовый отзыв."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8081"
DB = Path("/home/ubuntuii/courtdraft-ai/data/cases/courtdraft.db")
JSONL = Path("/home/ubuntuii/courtdraft-ai/data/feedback/feedback.jsonl")


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        cases = client.get("/api/cases", params={"limit": 200}).json()["items"]
        for case in cases:
            if str(case.get("case_number", "")).startswith("OCR-TEST"):
                response = client.delete(f"/api/cases/{case['id']}")
                print("deleted case", case["case_number"], response.status_code)

    connection = sqlite3.connect(DB)
    try:
        cur = connection.execute(
            "DELETE FROM feedback WHERE author_name = 'QA' AND comment LIKE 'Тест отзыва%'"
        )
        connection.commit()
        print("deleted feedback rows:", cur.rowcount)
    finally:
        connection.close()

    if JSONL.exists():
        keep = []
        for line in JSONL.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("author_name") == "QA" and str(
                row.get("comment", "")
            ).startswith("Тест отзыва"):
                continue
            keep.append(line)
        JSONL.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        print("jsonl rows left:", len(keep))


if __name__ == "__main__":
    main()
