"""Проверка конкурентной записи: 20 одновременных созданий дела."""

from __future__ import annotations

import asyncio
import uuid

import httpx

BASE = "http://127.0.0.1:8081"
COUNT = 20


async def main() -> int:
    prefix = uuid.uuid4().hex[:6]
    async with httpx.AsyncClient(base_url=BASE, timeout=120.0) as client:
        async def create(index: int) -> httpx.Response:
            return await client.post(
                "/api/cases",
                json={
                    "case_number": f"LOAD-{prefix}-{index}",
                    "court_name": "Нагрузочный тест",
                    "case_type": "civil",
                    "is_synthetic": True,
                    "student_code": "LOAD",
                },
            )

        responses = await asyncio.gather(
            *(create(index) for index in range(COUNT)), return_exceptions=True
        )
        ok = 0
        errors: list[str] = []
        created: list[str] = []
        for response in responses:
            if isinstance(response, httpx.Response) and response.status_code == 201:
                ok += 1
                created.append(response.json()["id"])
            else:
                errors.append(str(response)[:200])

        print(f"created {ok}/{COUNT}")
        for error in errors[:5]:
            print("error:", error)

        for case_id in created:
            await client.delete(f"/api/cases/{case_id}")
        print("cleaned", len(created))
    return 0 if ok == COUNT else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
