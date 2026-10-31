"""Размеры промптов разделов для дела DEMO-001.

Нужен, чтобы не гадать, почему 7B на маленьком num_ctx отвечает не по
делу: если промпт раздела длиннее контекста, Ollama обрезает его и
модель теряет инструкцию — ответ уходит в другой язык. Скрипт только
читает БД и считает символы; ничего не генерирует.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.agents.draft_agent import build_section_prompt  # noqa: E402
from app.agents.evidence_pack import load_template  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.session import create_engine_and_session  # noqa: E402
from app.models.entities import Case  # noqa: E402
from app.services.draft_service import DraftService  # noqa: E402
from app.agents.draft_agent import RULES_BUILT_SECTIONS  # noqa: E402


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = Settings().model_copy(
        update={
            "database_url": (
                "sqlite+aiosqlite:///"
                + (ROOT / "data" / "demo" / "output" / "real-run.db").as_posix()
            )
        }
    )
    async with create_engine_and_session(settings) as (_engine, factory):
        async with factory() as session:
            case = (
                await session.execute(select(Case).order_by(Case.created_at).limit(1))
            ).scalars().first()
            assert case is not None
            service = DraftService(session)
            pack = await service.build_pack(case.id)
            template = load_template(name="civil_judgment")
            print(f"дело: {case.case_number}")
            for section in template.get("sections", []):
                if section["key"] in RULES_BUILT_SECTIONS:
                    print(f"{section['key']:<14} правила, не модель")
                    continue
                prompt, _citations = build_section_prompt(section, pack)
                # Оценка токенов для русского текста: ~3 символа на токен.
                print(
                    f"{section['key']:<14} {len(prompt):>6} симв. "
                    f"≈ {len(prompt) // 3:>5} токенов"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
