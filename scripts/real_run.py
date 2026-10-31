"""REAL-прогон реальной модели: одна команда на понедельник.

Что делает:

1. проверяет готовность REAL (preflight): режим, Ollama, модель, память,
   пробный ответ. Не готова — не запускает генерацию и говорит почему;
2. берёт **копию** демо-БД (`data/demo/output/real-run.db`), а не рабочую
   и не демо-стенд: прогон не должен портить то, что уже проверено;
3. генерирует проект на реальной модели по делу с документами;
4. записывает `docs/real-run.json`: время по разделам, статус проверки,
   число замечаний. Текстов дел там нет — только ключи и числа;
5. различает три исхода: `0` — прогон состоялся, `2` — не готово
   (причина в отчёте), `3` — генерация упала или вернула пустые разделы.

Запуск на машине с RTX (после правки `.env`):

    backend\\.venv\\Scripts\\python.exe scripts\\real_run.py
    backend\\.venv\\Scripts\\python.exe scripts\\real_run.py --no-probe
    backend\\.venv\\Scripts\\python.exe scripts\\real_run.py --fresh --case "2-105/2025"

`--no-probe` не делает пробный запрос (на CPU он стоит десятки секунд).
Без пробы прогон всё равно проверяет, что текст разделов не пуст.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.session import create_all, create_engine_and_session  # noqa: E402
from app.llm.mock import MockLLMProvider
from app.llm.preflight import PreflightReport, default_probe, run_preflight  # noqa: E402
from app.models.entities import Case  # noqa: E402
from app.services.draft_service import DraftService  # noqa: E402

DEMO_DB = ROOT / "data" / "demo" / "output" / "demo.db"
RUN_DB = ROOT / "data" / "demo" / "output" / "real-run.db"
REPORT = ROOT / "docs" / "real-run.json"


def prepare_db(fresh: bool) -> Path:
    if not DEMO_DB.exists():
        raise SystemExit(
            f"нет демо-БД: {DEMO_DB}. Сначала: "
            "powershell -File scripts\\start-demo.ps1 -Rebuild"
        )
    if fresh or not RUN_DB.exists():
        RUN_DB.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEMO_DB, RUN_DB)
    return RUN_DB


def print_preflight(report: PreflightReport) -> None:
    for check in report.checks:
        print(f"[{check.state.value:<4}] {check.name:<14} {check.detail}")
        if check.hint:
            print(f"       -> {check.hint}")


def section_metrics(version: Any) -> list[dict[str, Any]]:
    """Разделы берутся из citations версии: это то, что видит судья."""
    raw_sections = version.citations if isinstance(version.citations, list) else []
    metrics: list[dict[str, Any]] = []
    for raw in raw_sections:
        if not isinstance(raw, dict):
            continue
        metrics.append(
            {
                "key": str(raw.get("key", "")),
                "name": str(raw.get("name", "")),
                "chars": len(str(raw.get("text", "") or "")),
                "latency_ms": int(raw.get("latency_ms", 0)),
                "is_mock": bool(raw.get("is_mock", False)),
                "needs_review": bool(raw.get("needs_review", False)),
                "error": str(raw.get("error", "")),
                "gaps": [str(gap) for gap in raw.get("gaps", []) or []],
            }
        )
    return metrics


async def real_run(
    case_number: str | None, *, with_probe: bool, self_test: bool = False
) -> int:
    prepare_db(fresh=False)
    settings = Settings().model_copy(
        update={"database_url": f"sqlite+aiosqlite:///{RUN_DB.as_posix()}"}
    )

    report = await run_preflight(
        settings, probe=default_probe if with_probe else None
    )
    print_preflight(report)
    report_path = REPORT.with_name("real-run-self-test.json") if self_test else REPORT
    if not report.ready and not self_test:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "date": time.strftime("%Y-%m-%d %H:%M"),
                    "preflight": report.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print("НЕ ГОТОВО: генерация не запускалась")
        return 2
    if self_test and not report.ready:
        print("Самопроверка: preflight не готов, идём на заглушке")

    started = time.perf_counter()
    async with create_engine_and_session(settings) as (engine, factory):
        await create_all(engine)
        async with factory() as session:
            query = select(Case).order_by(Case.created_at).limit(1)
            if case_number:
                query = select(Case).where(Case.case_number == case_number)
            case = (await session.execute(query)).scalars().first()
            if case is None:
                print(f"дело не найдено: {case_number or 'любое'}")
                return 3

            try:
                _draft, version, verification, _pack = await DraftService(
                    session
                ).generate(
                    case.id,
                    provider=MockLLMProvider() if self_test else None,
                    actor="real-run-self-test" if self_test else "real-run",
                )
            except Exception as exc:  # noqa: BLE001 - отчёт обязан назвать причину
                print(f"генерация упала: {exc}")
                return 3
            await session.commit()
            elapsed = int((time.perf_counter() - started) * 1000)
            sections = section_metrics(version)
            verification_row = {
                "status": verification.status.value,
                "issues": len(verification.issues),
                "critical": sum(
                    1
                    for issue in verification.issues
                    if issue.severity.value == "CRITICAL"
                ),
                "high": sum(
                    1 for issue in verification.issues if issue.severity.value == "HIGH"
                ),
                "claims_checked": verification.claims_checked,
                "unsupported_claims": verification.unsupported_claims,
            }
            case_number_value = case.case_number
            version_number = version.version

    result = {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "case_number": case_number_value,
        "version": version_number,
        "provider": settings.llm_provider.value,
        "model": settings.ollama_model,
        "elapsed_seconds": round(elapsed / 1000, 1),
        "sections": sections,
        "verification": verification_row,
        "preflight": report.to_dict(),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    empty = [item["key"] for item in sections if not item["chars"]]
    mocked = [item["key"] for item in sections if item["is_mock"]]
    failed = [item["key"] for item in sections if item["error"]]

    print()
    print(f"дело: {case_number_value}, версия {version_number}, {result['elapsed_seconds']} с")
    print(f"разделов: {len(sections)}, пустых: {len(empty)}, с ошибкой: {len(failed)}")
    print(
        f"проверка: {verification_row['status']}, замечаний "
        f"{verification_row['issues']} (CRITICAL {verification_row['critical']})"
    )
    print(f"отчёт: {report_path}")

    problems = []
    if mocked and not self_test:
        problems.append(f"разделы помечены как MOCK: {', '.join(mocked)}")
    if empty:
        problems.append(f"пустые разделы: {', '.join(empty)}")
    if failed:
        problems.append(f"разделы с ошибкой: {', '.join(failed)}")
    if problems:
        print("НЕ ПРОШЛО: " + "; ".join(problems))
        return 3
    if self_test:
        print("ПРОШЛО: конвейер прогона работает (заглушка, не модель)")
    else:
        print("ПРОШЛО: реальная модель дала текст по всем разделам")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--case", default=None)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="прогон на заглушке: проверить сам конвейер, не трогая модель",
    )
    args = parser.parse_args()

    if args.fresh:
        prepare_db(fresh=True)
    return asyncio.run(
        real_run(
            args.case,
            with_probe=not args.no_probe,
            self_test=args.self_test,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
