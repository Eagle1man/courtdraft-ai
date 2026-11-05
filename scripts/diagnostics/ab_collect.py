"""Собрать метрики генерации по фиксированному набору дел (A/B).

Зачем: «стало лучше» надо измерять, а не обсуждать. Скрипт прогоняет
одни и те же дела через API и складывает в JSON то, что можно сравнить:
статус проверки, число утверждений без источника, замечания по типам,
длину разделов и время на проект.

Модель задаётся не здесь: её меняют в `.env` стенда и перезапускают
службу. Скрипт запускается дважды — до и после — с разными метками.

Запуск:
    python scripts\\diagnostics\\ab_collect.py --label base1p5 ^
        --cases 3f2a...,9c1b...
    python scripts\\diagnostics\\ab_collect.py --label tuned7b ^
        --cases 3f2a...,9c1b...

Осторожно: формирование идёт синхронно (`wait=true`), одно дело может
занимать минуты. Запускать при остановленном тесте.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time

import requests

sys.stdout.reconfigure(errors="replace")

DEFAULT_API = "http://10.19.20.20:8081"


def _metrics(api: str, case_id: str) -> dict[str, object]:
    case = requests.get(f"{api}/api/cases/{case_id}", timeout=60).json()
    started = time.perf_counter()
    response = requests.post(
        f"{api}/api/cases/{case_id}/draft",
        params={"wait": "true"},
        json={"actor": "judge"},
        timeout=3600,
    )
    elapsed = time.perf_counter() - started
    if response.status_code != 202:
        return {
            "case_id": case_id,
            "case_number": case.get("case_number", ""),
            "error": f"{response.status_code}: {response.text[:300]}",
            "seconds": round(elapsed, 1),
        }

    draft = response.json()
    verification = draft.get("verification") or {}
    issues = verification.get("issues") or []
    by_type = collections.Counter(str(issue.get("type", "")) for issue in issues)
    sections = {
        str(section.get("key", "")): len(section.get("text", "") or "")
        for section in draft.get("sections") or []
    }
    return {
        "case_id": case_id,
        "case_number": case.get("case_number", ""),
        "case_type": case.get("case_type", ""),
        "status": verification.get("status", ""),
        "unsupported_claims": verification.get("unsupported_claims", 0),
        "claims_checked": verification.get("claims_checked", 0),
        "issues": dict(by_type),
        "issues_total": len(issues),
        "sections": sections,
        "seconds": round(elapsed, 1),
        "template": draft.get("template_name", ""),
        "model": draft.get("model", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="метка прогона")
    parser.add_argument("--cases", required=True, help="id дел через запятую")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--output", type=pathlib.Path, default=None)
    args = parser.parse_args()

    case_ids = [item.strip() for item in args.cases.split(",") if item.strip()]
    if not case_ids:
        raise SystemExit("Не указаны дела")

    rows = []
    for case_id in case_ids:
        print(f"дело {case_id} …")
        row = _metrics(args.api.rstrip("/"), case_id)
        rows.append(row)
        print(
            "  "
            + (
                row.get("error", "")
                or f"{row['status']} | {row['seconds']} c | замечаний {row['issues_total']}"
            )
        )

    output = args.output or pathlib.Path(f"ab-{args.label}.json")
    output.write_text(
        json.dumps({"label": args.label, "api": args.api, "cases": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"записано: {output}")


if __name__ == "__main__":
    main()
