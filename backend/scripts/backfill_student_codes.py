"""Восстановление кода участника у дел, созданных до появления колонки.

Зачем: `cases.student_code` появился позже самих дел, поэтому у всех
прежних строк код пустой. Пустой код означает «анонимное дело» — такое
видят все. Оставить так значило бы сохранить ту самую утечку, ради
которой колонка и добавлялась.

Код берётся не из догадки, а из журнала: при создании дела он писался
в `audit_entries.details.student_code`. Запись относится к тому же делу,
поэтому это факт, а не предположение.

Запуск:
    python -m scripts.backfill_student_codes --dry-run
    python -m scripts.backfill_student_codes
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import build_database_url
from app.models.entities import AuditEntry, Case


def _details(raw: Any) -> dict[str, Any]:
    """Разобрать details: в SQLite это может быть и строка, и dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


async def backfill(*, dry_run: bool) -> None:
    url = build_database_url()
    engine = create_async_engine(url)

    filled = 0
    already = 0
    no_source = 0

    try:
        async with engine.begin() as conn:
            cases = (
                await conn.execute(select(Case.id, Case.case_number, Case.student_code))
            ).all()

            for case_id, case_number, current in cases:
                if (current or "").strip():
                    already += 1
                    continue

                entries = (
                    await conn.execute(
                        select(AuditEntry.details)
                        .where(
                            AuditEntry.action == "case.created",
                            AuditEntry.case_id == case_id,
                        )
                        .order_by(AuditEntry.id.desc())
                    )
                ).scalars().all()

                code = ""
                for entry in entries:
                    code = str(_details(entry).get("student_code") or "").strip()
                    if code:
                        break

                if not code:
                    no_source += 1
                    continue

                if dry_run:
                    print(f"[dry-run] {case_number}: {code!r}")
                else:
                    await conn.execute(
                        update(Case).where(Case.id == case_id).values(student_code=code)
                    )
                filled += 1
    finally:
        await engine.dispose()

    print(
        f"заполнено: {filled}, уже было: {already}, "
        f"источника нет: {no_source} (останутся анонимными)"
    )
    if dry_run:
        print("это был dry-run, база не изменена")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="только показать")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
