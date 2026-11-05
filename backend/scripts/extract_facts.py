"""Извлечь факты из документов дела и показать реестр.

Инструмент для замеров: один и тот же вход, разный режим извлечения
(один вызов или по категориям), разная модель — и сравнимые цифры.

Запуск:
    python -m scripts.extract_facts --file путь\\документ.docx --model qwen2.5:1.5b
    python -m scripts.extract_facts --dir путь\\к\\делу --mode categories --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

from app.config import LLMProviderName, Settings
from app.extraction.fact_extractor import extract_facts
from app.extraction.schemas import ExtractionStatus
from app.ingestion.chunking import chunk_text
from app.ingestion.extract import extract_text
from app.llm import build_llm_provider
from app.validation.evidence import verify_registry

sys.stdout.reconfigure(errors="replace")

SUPPORTED = {".docx", ".pdf", ".txt", ".md", ".html", ".htm"}


def _settings(model: str) -> Settings:
    return Settings(
        llm_provider=LLMProviderName.OLLAMA,
        ollama_enabled=True,
        ollama_model=model,
        rag_laws_enabled=False,
    )


def _files(path: pathlib.Path) -> list[pathlib.Path]:
    if path.is_dir():
        return sorted(item for item in path.rglob("*") if item.suffix.lower() in SUPPORTED)
    return [path]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=pathlib.Path, default=None)
    parser.add_argument("--dir", type=pathlib.Path, default=None)
    parser.add_argument("--model", default="qwen2.5:1.5b")
    parser.add_argument("--mode", choices=["single", "categories"], default="single")
    parser.add_argument("--max-chars", type=int, default=6000)
    parser.add_argument("--json", type=pathlib.Path, default=None)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    target = args.file or args.dir
    if target is None:
        raise SystemExit("укажите --file или --dir")

    files = _files(target)
    if args.limit:
        files = files[: args.limit]
    if not files:
        raise SystemExit("нет подходящих файлов")

    provider = build_llm_provider(_settings(args.model), model=args.model)
    print(f"модель: {args.model} | режим: {args.mode} | файлов: {len(files)}")

    results = []
    for path in files:
        started = time.perf_counter()
        data = path.read_bytes()
        extracted = extract_text(path.name, data)
        chunks = chunk_text(extracted.text, document_id=path.name)
        run = await extract_facts(
            provider,
            chunks,
            document_id=path.name,
            max_chars=args.max_chars,
            mode=args.mode,
        )
        report = verify_registry(run.registry, {path.name: extracted.text})
        elapsed = round(time.perf_counter() - started, 1)

        registry = run.registry
        print(
            f"{path.name[:50]:52} "
            f"фактов {len(registry.facts):3} "
            f"сумм {len(registry.amounts):3} "
            f"дат {len(registry.dates):3} "
            f"участн. {len(registry.participants):2} "
            f"цитат точно {report.exact:2}/норм {report.normalized:2}/нет {report.absent:2} "
            f"статус {registry.status.value:12} {elapsed:5.1f} c"
        )
        results.append(
            {
                "file": path.name,
                "seconds": elapsed,
                "status": registry.status.value,
                "counts": {
                    "facts": len(registry.facts),
                    "amounts": len(registry.amounts),
                    "dates": len(registry.dates),
                    "participants": len(registry.participants),
                    "claims": len(registry.claims),
                },
                "quotes": {
                    "exact": report.exact,
                    "normalized": report.normalized,
                    "absent": report.absent,
                },
                "missing": [item.model_dump() for item in registry.missing],
                "registry": registry.model_dump(mode="json"),
            }
        )

    ok = sum(1 for item in results if item["status"] == ExtractionStatus.OK.value)
    print(f"\nитого: {len(results)} документов, без замечаний {ok}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"подробно: {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
