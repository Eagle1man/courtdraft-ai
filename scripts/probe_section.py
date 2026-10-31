"""Сырой ответ модели на промпт одного раздела — для разбора отказов.

Показывает, что именно вернула модель и почему текст признан непригодным.
Нужен, когда в проекте «раздел не сформирован», а причина неясна:
«не тот язык», «рассуждения», «слишком коротко» — разные болезни.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\probe_section.py descriptive
    backend\\.venv\\Scripts\\python.exe scripts\\probe_section.py motivating --num-ctx 4096

Текст пишется в artifacts\\probe-<раздел>.txt (демо-дело синтетическое,
персональных данных нет).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.agents.draft_agent import (  # noqa: E402
    NEEDS_REVIEW,
    NO_LAW_FOUND,
    SYSTEM_PROMPT,
    build_section_prompt,
)
from app.agents.evidence_pack import load_template  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.session import create_engine_and_session  # noqa: E402
from app.drafting.section_quality import unusable_reason  # noqa: E402
from app.llm.base import LLMMessage, LLMRequest, LLMRole  # noqa: E402
from app.llm.ollama_provider import OllamaProvider  # noqa: E402
from app.models.entities import Case  # noqa: E402
from app.services.draft_service import DraftService  # noqa: E402


def cyrillic_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    cyrillic = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    return cyrillic / len(letters)


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("section")
    parser.add_argument("--num-ctx", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=420)
    args = parser.parse_args()

    settings = Settings()
    if args.num_ctx:
        settings = settings.model_copy(update={"ollama_num_ctx": args.num_ctx})
    settings = settings.model_copy(
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
            pack = await DraftService(session).build_pack(case.id)
            template = load_template(name="civil_judgment")
            section = next(
                item
                for item in template.get("sections", [])
                if item["key"] == args.section
            )
            prompt, _citations = build_section_prompt(section, pack)

    provider = OllamaProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        timeout_sec=settings.ollama_timeout_sec,
        num_ctx=settings.ollama_num_ctx,
        temperature=settings.ollama_temperature,
    )
    request = LLMRequest(
        messages=[
            LLMMessage(
                role="system",
                content=SYSTEM_PROMPT.format(review=NEEDS_REVIEW, no_law=NO_LAW_FOUND),
            ),
            LLMMessage(role="user", content=prompt),
        ],
        role=LLMRole.GENERATION,
        temperature=0.1,
        max_tokens=args.max_tokens,
    )

    started = time.perf_counter()
    response = await provider.generate(request)
    elapsed = time.perf_counter() - started

    raw = response.text
    out = ROOT / "artifacts" / f"probe-{args.section}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw, encoding="utf-8")

    print(f"модель: {settings.ollama_model}, num_ctx={settings.ollama_num_ctx}")
    print(f"время: {elapsed:.1f} с, символов: {len(raw)}, "
          f"кириллицы: {cyrillic_ratio(raw):.0%}")
    print(
        f"finish_reason={response.finish_reason}, "
        f"prompt_tokens={response.usage.prompt_tokens}, "
        f"completion_tokens={response.usage.completion_tokens}"
    )
    print(f"причина отказа: {unusable_reason(raw) or '— текст пригоден'}")
    print(f"сырой текст: {out}")
    print("--- первые 400 символов ---")
    print(raw[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
