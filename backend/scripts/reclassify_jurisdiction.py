"""Переопределение вида судопроизводства у дел, созданных до этой правки.

Зачем: интерфейс раньше всегда отправлял `case_type = "civil"`, поэтому у
всех прежних дел стоит «гражданское» — включая арбитражные. Генератор
теперь выбирает шаблон по `case_type`, и без пересчёта старые арбитражные
дела продолжили бы получать ГПК РФ.

Тип определяется по материалам теми же правилами, что и при загрузке
(`app.ingestion.jurisdiction`): шапка «Арбитражный суд», номер «А40-…»,
ссылки на АПК РФ. Дела, где признаков нет или они противоречат друг
другу, не трогаются — угадывать тип нельзя.

Запуск (на стенде, из каталога backend):
    .venv/bin/python -m scripts.reclassify_jurisdiction --dry-run
    .venv/bin/python -m scripts.reclassify_jurisdiction
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import build_database_url
from app.ingestion.jurisdiction import detect_jurisdiction
from app.models.entities import Case, CaseDocument


async def reclassify(*, dry_run: bool) -> None:
    url = build_database_url()
    engine = create_async_engine(url)

    changed = 0
    same = 0
    undecided = 0
    empty = 0

    try:
        async with engine.begin() as conn:
            cases = (
                await conn.execute(
                    select(Case.id, Case.case_number, Case.case_type).where(
                        Case.is_archived.is_(False)
                    )
                )
            ).all()

            for case_id, case_number, current in cases:
                texts = (
                    await conn.execute(
                        select(CaseDocument.text).where(CaseDocument.case_id == case_id)
                    )
                ).scalars().all()
                texts = [text or "" for text in texts]
                if not any(text.strip() for text in texts):
                    empty += 1
                    continue

                verdict = detect_jurisdiction(texts)
                if not verdict.detected:
                    undecided += 1
                    continue
                if verdict.case_type == (current or "").strip().lower():
                    same += 1
                    continue

                confidence = verdict.confidence or "?"
                reasons = "; ".join(verdict.reasons[:3])
                print(
                    f"{case_number}: {current or '—'} -> {verdict.case_type} "
                    f"({confidence}: {reasons})"
                )
                if not dry_run:
                    await conn.execute(
                        update(Case)
                        .where(Case.id == case_id)
                        .values(case_type=verdict.case_type)
                    )
                changed += 1
    finally:
        await engine.dispose()

    print(
        f"изменено: {changed}, уже верно: {same}, "
        f"признаков нет: {undecided}, без материалов: {empty}"
    )
    if dry_run:
        print("это был dry-run, база не изменена")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="только показать")
    args = parser.parse_args()
    asyncio.run(reclassify(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
