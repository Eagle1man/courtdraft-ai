"""Задачи выходного прогона. Каждая возвращает словарь для отчёта.

Правила, общие для всех задач:

* ничего не менять в данных дел, кроме временного дела, которое тут же
  удаляется (задача `fuzz`);
* не трогать файловый Qdrant напрямую: пока служба работает, второй
  клиент на тот же каталог открыть нельзя, а останавливать её ради
  замера нельзя;
* каждый сбой задачи фиксируется в результате, а не роняет весь прогон.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from app.extraction.fact_extractor import extract_facts
from app.validation.evidence import verify_registry


@dataclass(frozen=True)
class TextChunk:
    """Фрагмент документа для извлечения."""

    text: str
    page: int | None = None
    paragraph: int | None = None


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20)


def _percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return round(ordered[index], 3)


def pick_documents(rows: list[tuple], *, per_type: int = 4, limit: int = 24) -> list[tuple]:
    """Выбрать документы для замера: поровну от каждого типа.

    Один тип не должен забить выборку: договоры и иски ведут себя
    по-разному, и «средняя точность по корпусу» без разбивки скрывает,
    что на одном типе всё хорошо, а на другом пусто.
    """
    by_type: dict[str, list[tuple]] = {}
    for row in rows:
        by_type.setdefault(str(row[3] or "other"), []).append(row)
    picked: list[tuple] = []
    for doc_type in sorted(by_type):
        picked.extend(by_type[doc_type][:per_type])
    return picked[:limit]


# ── 1. Тесты ─────────────────────────────────────────────────────────────


def task_tests(backend: Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Прогнать тесты и линтер в окружении самого стенда.

    Локально Python 3.14, на стенде — 3.12. Разница версий уже один раз
    ловила сюрпризы, поэтому проверять надо там, где код работает.
    """
    started = time.perf_counter()
    pytest = subprocess.run(  # noqa: S603 - путь к python свой, аргументы фиксированы
        [str(backend / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:warnings"],
        cwd=backend,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    ruff = subprocess.run(  # noqa: S603 - то же самое, команда неизменна
        [str(backend / ".venv/bin/python"), "-m", "ruff", "check", "app", "tests"],
        cwd=backend,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    tail = (pytest.stdout or "").strip().splitlines()[-3:]
    return {
        "pytest_code": pytest.returncode,
        "pytest_tail": tail,
        "ruff_code": ruff.returncode,
        "ruff_tail": (ruff.stdout or "").strip().splitlines()[-3:],
        "minutes": round((time.perf_counter() - started) / 60, 1),
    }


# ── 2. Целостность и уборка ──────────────────────────────────────────────


def task_integrity(db_path: Path, backups_dir: Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Проверить базу и почистить старые резервные копии."""
    con = _connect(db_path)
    try:
        integrity = con.execute("pragma integrity_check").fetchone()[0]
        foreign = con.execute("pragma foreign_key_check").fetchall()
        counts = {
            # Имена таблиц — из закрытого списка ниже, не из запроса.
            table: con.execute(
                f"select count(*) from {table}"  # noqa: S608 - список фиксирован
            ).fetchone()[0]
            for table in (
                "cases",
                "case_documents",
                "document_chunks",
                "drafts",
                "draft_versions",
                "verifications",
                "jobs",
                "case_fact_registries",
            )
        }
        orphans = {
            "chunks_without_document": con.execute(
                "select count(*) from document_chunks c "
                "left join case_documents d on d.id = c.document_id where d.id is null"
            ).fetchone()[0],
            "documents_without_case": con.execute(
                "select count(*) from case_documents d "
                "left join cases c on c.id = d.case_id where c.id is null"
            ).fetchone()[0],
            "jobs_without_case": con.execute(
                "select count(*) from jobs j "
                "left join cases c on c.id = j.case_id where c.id is null"
            ).fetchone()[0],
        }
    finally:
        con.close()

    removed = 0
    if backups_dir.exists():
        files = sorted(
            backups_dir.glob("courtdraft-*.db"), key=lambda item: item.stat().st_mtime
        )
        for old in files[:-5]:
            old.unlink(missing_ok=True)
            removed += 1

    return {
        "integrity": integrity,
        "foreign_key_violations": len(foreign),
        "counts": counts,
        "orphans": orphans,
        "backups_removed": removed,
    }


# ── 3. Извлечение фактов по корпусу ──────────────────────────────────────


async def _extract_one(
    doc: tuple, chunks: list[TextChunk], provider: Any, timeout_sec: int
) -> dict[str, Any]:
    doc_id, filename, doc_type, text = doc[0], doc[1], doc[3], doc[4]
    started = time.perf_counter()
    try:
        run = await asyncio.wait_for(
            extract_facts(
                provider,
                chunks,
                document_id=str(doc_id),
                mode="categories",
            ),
            timeout=timeout_sec,
        )
    except TimeoutError:
        return {
            "document": filename,
            "type": doc_type,
            "status": "timeout",
            "seconds": timeout_sec,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "document": filename,
            "type": doc_type,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }

    report = verify_registry(run.registry, {str(doc_id): text})
    registry = run.registry
    return {
        "document": filename,
        "type": doc_type,
        "status": run.status.value,
        "seconds": round(time.perf_counter() - started, 1),
        "participants": len(registry.participants),
        "amounts": len(registry.amounts),
        "dates": len(registry.dates),
        "claims": len(registry.claims),
        "quotes_exact": report.exact,
        "quotes_normalized": report.normalized,
        "quotes_absent": report.absent,
        "missing": len(registry.missing),
        "errors": run.errors[:3],
    }


def task_extract_bench(
    db_path: Path, output: Path, *_args: Any, **_kwargs: Any
) -> dict[str, Any]:
    """Извлечь факты из реальных документов дела и проверить цитаты.

    Это замер этапа 2 на живом корпусе: сколько сумм и дат находится,
    какая доля цитат подтверждается текстом. Модель — FACTS_MODEL,
    то есть та же, что у студентов.
    """
    from app.config import get_settings
    from app.ingestion.chunking import chunk_text
    from app.llm import build_llm_provider

    cfg = get_settings()
    model = cfg.facts_model or cfg.ollama_model

    con = _connect(db_path)
    try:
        rows = con.execute(
            "select id, filename, case_id, document_type, text "
            "from case_documents where length(text) > 800 "
            "order by document_type, length(text) desc"
        ).fetchall()
    finally:
        con.close()

    picked = pick_documents(rows)
    if not picked:
        return {"documents": 0, "note": "в базе нет документов достаточной длины"}

    provider = build_llm_provider(cfg, model=model)
    results: list[dict[str, Any]] = []

    con = _connect(db_path)
    try:
        for row in picked:
            stored = con.execute(
                "select text, page, paragraph from document_chunks "
                "where document_id = ? order by chunk_index",
                (row[0],),
            ).fetchall()
            chunks = [
                TextChunk(text=item[0], page=item[1], paragraph=item[2])
                for item in stored
                if (item[0] or "").strip()
            ]
            if not chunks:
                chunks = [
                    TextChunk(text=piece.text)
                    for piece in chunk_text(row[4], document_id=str(row[0]))
                ]
            if not chunks:
                continue
            result = asyncio.run(_extract_one(row, chunks, provider, timeout_sec=300))
            results.append(result)
            (output / "extract_partial.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        con.close()

    finished = [item for item in results if "seconds" in item and item.get("status") != "timeout"]
    return {
        "model": model,
        "documents": len(results),
        "ok": sum(1 for item in finished if item.get("status") == "ok"),
        "needs_review": sum(1 for item in finished if item.get("status") == "needs_review"),
        "timeouts": sum(1 for item in results if item.get("status") == "timeout"),
        "errors": sum(1 for item in results if item.get("status") == "error"),
        "avg_seconds": round(
            sum(item["seconds"] for item in finished) / len(finished), 1
        )
        if finished
        else 0,
        "amounts": sum(item.get("amounts", 0) for item in finished),
        "dates": sum(item.get("dates", 0) for item in finished),
        "claims": sum(item.get("claims", 0) for item in finished),
        "quotes_exact": sum(item.get("quotes_exact", 0) for item in finished),
        "quotes_absent": sum(item.get("quotes_absent", 0) for item in finished),
        "details": results,
    }


# ── 4. Устойчивость загрузки ─────────────────────────────────────────────


def task_fuzz(base_url: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Проверить, что на плохие файлы служба отвечает 4xx, а не 500.

    Студенты приносят что угодно: пустой файл, обрезанный docx, файл
    с чужим расширением. Пятисотка на такой вход — это сбой, который
    выглядит как «программа сломалась».
    """
    import httpx

    cases: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url, timeout=60) as client:
        created = client.post(
            "/cases",
            json={
                "case_number": f"WEEKEND-{datetime.now():%H%M%S}",
                "court_name": "Проверка",
                "case_type": "civil",
                "category": "",
                "is_synthetic": True,
            },
        )
        if created.status_code != 201:
            return {"error": f"не удалось создать дело: {created.status_code}"}
        case_id = created.json()["id"]

        samples = [
            ("пустой.txt", b"", 400),
            ("битый.docx", b"PK\x03\x04broken", 400),
            ("не тот тип.exe", b"MZ....", 400),
            ("нормальный.txt", ("Истец просит взыскать 1000 рублей. " * 40).encode(), 201),
        ]
        try:
            for name, payload, expected in samples:
                response = client.post(
                    f"/cases/{case_id}/documents/upload",
                    files={"file": (name, payload, "application/octet-stream")},
                )
                cases.append(
                    {
                        "file": name,
                        "status": response.status_code,
                        "expected": expected,
                        "ok": response.status_code == expected,
                    }
                )
        finally:
            client.delete(f"/cases/{case_id}")

    return {
        "checks": cases,
        "passed": sum(1 for item in cases if item["ok"]),
        "failed": sum(1 for item in cases if not item["ok"]),
    }


# ── 5. Задержки API ──────────────────────────────────────────────────────


def task_perf(base_url: str, db_path: Path, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Замерить задержки чтения: список дел, документы, здоровье."""
    import httpx

    con = _connect(db_path)
    try:
        row = con.execute(
            "select case_id from case_documents order by length(text) desc limit 1"
        ).fetchone()
    finally:
        con.close()
    case_id = row[0] if row else ""

    endpoints = ["/health", "/info", "/cases?limit=20"]
    if case_id:
        endpoints.append(f"/cases/{case_id}/documents")

    report: dict[str, Any] = {}
    with httpx.Client(base_url=base_url, timeout=30) as client:
        for endpoint in endpoints:
            times: list[float] = []
            codes: list[int] = []
            for _ in range(20):
                started = time.perf_counter()
                try:
                    response = client.get(endpoint)
                    codes.append(response.status_code)
                except Exception:  # noqa: BLE001
                    codes.append(0)
                times.append((time.perf_counter() - started) * 1000)
            report[endpoint] = {
                "p50_ms": round(median(times), 1),
                "p95_ms": _percentile(times, 0.95),
                "codes": sorted(set(codes)),
            }
    return report


TASKS = {
    "tests": task_tests,
    "integrity": task_integrity,
    "extract_bench": task_extract_bench,
    "fuzz": task_fuzz,
    "perf": task_perf,
}
