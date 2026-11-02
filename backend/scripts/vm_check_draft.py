"""Краткая проверка сформированного проекта через API."""

from __future__ import annotations

import json

import httpx

CASE = "72a3ed02-99cc-4cb2-84b7-d31f8cded105"
BASE = "http://127.0.0.1:8081"


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        response = client.get(f"/api/cases/{CASE}/draft")
        print("status:", response.status_code)
        data = response.json()
        if not data:
            print("NO DRAFT")
            return 1
        print("draft_id:", data.get("id"), "version:", data.get("current_version"))
        print("status:", data.get("status"), "template:", data.get("template_name"))
        text = data.get("text") or ""
        sections = data.get("sections") or []
        print("chars:", len(text), "sections:", len(sections))
        print("sources:", len(data.get("sources") or []))
        verification = data.get("verification") or {}
        print(
            "verification:",
            verification.get("status"),
            "issues:",
            len(verification.get("issues") or []),
            "checked:",
            verification.get("sections_checked"),
        )
        print("--- head ---")
        print(text[:400].replace("\n", " | "))
        print("--- section titles ---")
        for section in sections:
            title = section.get("title") or section.get("name") or ""
            body = section.get("text") or ""
            print(f"- {title} ({len(body)} chars)")
        issues = verification.get("issues") or []
        if issues:
            print("--- first issues ---")
            for issue in issues[:5]:
                print("-", json.dumps(issue, ensure_ascii=False)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
