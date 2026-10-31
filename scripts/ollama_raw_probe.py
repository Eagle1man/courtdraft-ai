"""Сырой ответ Ollama на промпт раздела: что именно пришло в JSON.

Нужен, когда провайдер видит пустой текст, а непонятно почему: пустой
`content` бывает от отказа модели, от обрезки по контексту и от того, что
ответ ушёл в отдельное поле размышления (`thinking`). Это разные
причины, и лечатся они по-разному.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\ollama_raw_probe.py descriptive --num-ctx 4096
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.draft_agent import (  # noqa: E402
    NEEDS_REVIEW,
    NO_LAW_FOUND,
    SYSTEM_PROMPT,
    build_section_prompt,
)
from app.agents.evidence_pack import load_template  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.session import create_engine_and_session  # noqa: E402
from app.models.entities import Case  # noqa: E402
from app.services.draft_service import DraftService  # noqa: E402


async def load_prompt(section_key: str, settings: Settings) -> str:
    async with create_engine_and_session(settings) as (_engine, factory):
        async with factory() as session:
            case = (
                await session.execute(select(Case).order_by(Case.created_at).limit(1))
            ).scalars().first()
            assert case is not None
            pack = await DraftService(session).build_pack(case.id)
            template = load_template(name="civil_judgment")
            section = next(
                item for item in template["sections"] if item["key"] == section_key
            )
            prompt, _citations = build_section_prompt(section, pack)
            return prompt


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("section")
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--max-tokens", type=int, default=420)
    parser.add_argument("--reasoning", action="store_true")
    parser.add_argument("--suffix", default="", help="добавить текст в конец промпта")
    parser.add_argument("--db", default=None, help="путь к SQLite-базе (по умолчанию real-run.db)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else ROOT / "data" / "demo" / "output" / "real-run.db"
    settings = Settings().model_copy(
        update={
            "ollama_num_ctx": args.num_ctx,
            "database_url": f"sqlite+aiosqlite:///{db_path.as_posix()}",
        }
    )
    prompt = await load_prompt(args.section, settings)
    if args.suffix:
        prompt = f"{prompt}\n\n{args.suffix}"

    payload = {
        "model": settings.ollama_model,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(
                    review=NEEDS_REVIEW, no_law=NO_LAW_FOUND
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": args.reasoning,
        "options": {
            "num_ctx": args.num_ctx,
            "temperature": 0.1,
            "num_predict": args.max_tokens,
        },
    }
    async with httpx.AsyncClient(timeout=1800) as client:
        response = await client.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload
        )
        response.raise_for_status()
        data = response.json()

    message = data.get("message") or {}
    print(f"ключи ответа: {sorted(data)}")
    print(f"ключи message: {sorted(message)}")
    print(f"done_reason={data.get('done_reason')}, eval_count={data.get('eval_count')}")
    print(f"content: {len(message.get('content') or '')} символов")
    print(f"thinking: {len(message.get('thinking') or '')} символов")
    print("--- content ---")
    print((message.get("content") or "")[:300])
    print("--- thinking ---")
    print((message.get("thinking") or "")[:300])

    out = ROOT / "artifacts" / f"ollama-raw-{args.section}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"сырой JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
