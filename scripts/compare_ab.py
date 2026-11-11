#!/usr/bin/env python3
"""Сравнить несколько отчётов eval_deepseek_quality по одним и тем же делам."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def metrics(data: dict[str, Any]) -> dict[str, Any]:
    cases = data.get("cases") or []
    return {
        "cases": len(cases),
        "p0_cases": sum(1 for case in cases if case.get("p0")),
        "review_cases": sum(
            1 for case in cases if "ТРЕБУЕТ ПРОВЕРКИ" in (case.get("operative_text") or "")
        ),
        "empty_operative": sum(
            1
            for case in cases
            if "не раскрыта" in (case.get("operative_text") or "")
            or "не сформирована" in (case.get("operative_text") or "")
        ),
        "severe": sum(
            int((case.get("verification") or {}).get("severe_issues", 0))
            for case in cases
        ),
        "issues": sum(
            len((case.get("verification") or {}).get("issues", [])) for case in cases
        ),
        "spent": round(float(data.get("spent_rub") or 0), 4),
        "tokens": int(data.get("prompt_tokens") or 0)
        + int(data.get("completion_tokens") or 0),
        "avg_sec": round(
            sum(float(case.get("elapsed_sec") or 0) for case in cases)
            / max(1, len(cases)),
            1,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", help="label=path")
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "cd" / "ab-summary.md"))
    args = parser.parse_args()

    rows: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for item in args.reports:
        label, _, raw_path = item.partition("=")
        path = Path(raw_path)
        data = load(path)
        rows.append((label or path.stem, metrics(data), data))

    lines = ["# A/B сравнение", ""]
    lines.append(
        "| Вариант | Дел | P0 | REVIEW | Пустая резолютивная | "
        "Тяжёлых | Токены | ₽ | сек/дело |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, metric, _data in rows:
        lines.append(
            f"| {label} | {metric['cases']} | {metric['p0_cases']} | "
            f"{metric['review_cases']} | {metric['empty_operative']} | "
            f"{metric['severe']} | {metric['tokens']} | {metric['spent']} | "
            f"{metric['avg_sec']} |"
        )
    lines.append("")
    lines.append("## По делам")
    lines.append("")
    labels = [label for label, _metric, _data in rows]
    lines.append("| Дело | " + " | ".join(labels) + " |")
    lines.append("|---|" + "---|" * len(labels))
    case_names = sorted(
        {
            case.get("case")
            for _label, _metric, data in rows
            for case in (data.get("cases") or [])
        }
    )
    by_label = {
        label: {case.get("case"): case for case in (data.get("cases") or [])}
        for label, _metric, data in rows
    }
    for name in case_names:
        cells = []
        for label in labels:
            case = by_label[label].get(name)
            if case is None:
                cells.append("—")
            else:
                severe = (case.get("verification") or {}).get("severe_issues", 0)
                cost = case.get("cost_rub", 0)
                cells.append(f"P0={len(case.get('p0') or [])}, severe={severe}, ₽{cost:.4f}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"написано: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
