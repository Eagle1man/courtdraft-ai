"""Метрики качества паспорт-модуля на синтетическом наборе (ТЗ §18).

Запуск: python scripts/passport_eval.py [каталог]
По умолчанию используется FakeLLM: платные вызовы запрещены без явного
PASSPORT_LLM_MODE=host/openai.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.passport_ai.llmclient import make_llm  # noqa: E402
from app.passport_ai.service import PassportService  # noqa: E402


def field_metrics(expected: dict, fields: list[dict]) -> dict:
    tp = fp = fn = 0
    for field in fields:
        exp = expected.get(field["role"])
        got = field.get("value")
        if exp is not None and got is not None:
            if str(exp) == str(got):
                tp += 1
            else:
                fp += 1
                fn += 1
        elif exp is not None and got is None:
            fn += 1
        elif exp is None and got is not None:
            fp += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
    }


def run_case(service: PassportService, case_dir: Path) -> dict:
    files = [
        (path.name, path.read_bytes())
        for path in sorted(case_dir.glob("in_*"))
    ]
    started = time.perf_counter()
    asyncio.run(service.run(case_dir.name, files))
    analysis = service.analysis(case_dir.name)
    expected = json.loads((case_dir / "expected.json").read_text("utf-8"))
    metrics = field_metrics(expected["fields"], analysis["fields"])
    evidence_fields = [
        field for field in analysis["fields"] if field["value"] not in (None, "")
    ]
    evidence_coverage = (
        sum(1 for field in evidence_fields if field["fact_ids"])
        / len(evidence_fields)
        if evidence_fields
        else 0.0
    )
    unsupported = sum(
        1 for fact in analysis["facts"] if fact["status"] == "UNSUPPORTED"
    )
    expected_conflicts = expected.get("conflicts", [])
    found_conflicts = analysis["conflicts"]
    conflict_recall = (
        len(
            [
                conflict
                for conflict in expected_conflicts
                if any(item["role"] == conflict for item in found_conflicts)
            ]
        )
        / len(expected_conflicts)
        if expected_conflicts
        else 1.0
    )
    correct_empty = sum(
        1
        for role, value in expected["fields"].items()
        if value is None
        and all(
            field["value"] in (None, "")
            for field in analysis["fields"]
            if field["role"] == role
        )
    )
    total_empty = sum(1 for value in expected["fields"].values() if value is None)
    return {
        "case": case_dir.name,
        **metrics,
        "evidence_coverage": round(evidence_coverage, 3),
        "unsupported_values": unsupported,
        "conflict_recall": round(conflict_recall, 3),
        "correct_empty": f"{correct_empty}/{total_empty}",
        "time_sec": round(time.perf_counter() - started, 3),
    }


def main() -> int:
    fixtures = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "passport_eval"
    if not fixtures.exists():
        print(f"нет каталога {fixtures}")
        return 1
    contract = json.loads(
        (ROOT / "artifacts" / "passport" / "passport_contract.json").read_text(
            "utf-8"
        )
    )
    field_map = json.loads(
        (ROOT / "artifacts" / "passport" / "field_map.json").read_text("utf-8")
    )
    service = PassportService(
        ROOT / "artifacts" / "passport" / "run",
        make_llm(),
        contract,
        field_map,
    )
    rows = [
        run_case(service, directory)
        for directory in sorted(fixtures.iterdir())
        if directory.is_dir()
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
