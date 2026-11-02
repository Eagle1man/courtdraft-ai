"""Проверка OCR через HTTP: загрузить скан и посмотреть источник текста."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8081"
SCAN = Path("/home/ubuntuii/courtdraft-ai/data/ocr_scan.pdf")


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=300.0) as client:
        created = client.post(
            "/api/cases",
            json={
                "case_number": f"OCR-TEST-{uuid.uuid4().hex[:8]}",
                "court_name": "Суд судебного участка № 123",
                "case_type": "civil",
                "category": "",
                "is_synthetic": True,
            },
        )
        created.raise_for_status()
        case_id = created.json()["id"]
        print("case:", case_id)

        upload = client.post(
            f"/api/cases/{case_id}/documents/upload",
            files={"file": (SCAN.name, SCAN.read_bytes(), "application/pdf")},
        )
        print("upload:", upload.status_code, upload.text[:400])
        if upload.status_code >= 400:
            return 1

        doc = upload.json()
        doc_id = doc["id"]
        print(
            "document:",
            doc.get("filename"),
            "chars=",
            doc.get("characters"),
            "source=",
            doc.get("text_source"),
            "warnings=",
            doc.get("warnings"),
        )

        detail = client.get(f"/api/cases/{case_id}/documents/{doc_id}")
        detail.raise_for_status()
        text = detail.json().get("text", "")
        print("text head:", text[:300].replace("\n", " | "))
        if doc.get("text_source") != "ocr":
            print("FAIL: text_source is not ocr")
            return 1
        if not text.strip():
            print("FAIL: empty text")
            return 1
    print("OCR OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
