#!/usr/bin/env python3
"""Собрать демонстрационный пакет для судей из готовых API-прогонов.

Пакет — не судебный акт и не заключение. Он показывает: материалы (кратко),
проект ИИ, решение суда по тому же делу и замечания проверки. Сырые тексты
берутся из artifacts/, в git не коммитятся.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GLOB = "artifacts/deepseek-quality-*/report.json"


def load_case(case: str, reports: list[Path]) -> dict[str, Any] | None:
    for path in reports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in data.get("cases") or []:
            if item.get("case") == case:
                return item
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="+", default=["case-018", "case-010", "case-034"])
    parser.add_argument("--out", default=str(ROOT / "docs" / "judge-demo-pack-2026-11-11.md"))
    args = parser.parse_args()
    reports = sorted(
        ROOT.glob(DEFAULT_GLOB),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    lines: list[str] = []
    lines.append("# Демонстрационный пакет для судей")
    lines.append("")
    lines.append("**Это не судебный акт и не юридическое заключение.**")
    lines.append(
        "Ниже — проекты, которые система построила по учебным (обезличенным) "
        "материалам, и решения суда по тем же делам. Каждый проект требует "
        "проверки судьёй; система не заменяет судью и не выносит решение."
    )
    lines.append("")
    lines.append("## Как читать")
    lines.append("")
    lines.append("- «Проект ИИ» — то, что система предлагает; это черновик.")
    lines.append("- «Решение суда» — состоявшийся акт по тому же делу.")
    lines.append("- «Проверка» — автоматические замечания; они не заменяют правовую оценку.")
    lines.append("- Суммы и исходы сверяйте самостоятельно: совпадение не доказывает правоту.")
    lines.append("")

    for case in args.cases:
        item = load_case(case, reports)
        if item is None:
            lines.append(f"## {case}")
            lines.append("")
            lines.append("Нет данных в artifacts.")
            lines.append("")
            continue
        lines.append(f"## {case}")
        lines.append("")
        lines.append("### Кратко")
        lines.append(f"- Исход ИИ: {', '.join(item.get('outcome_system') or []) or '—'}")
        lines.append(f"- Исход суда: {', '.join(item.get('outcome_court') or []) or '—'}")
        comparison = item.get("comparison") or {}
        lines.append(
            f"- Суммы суда, найденные в проекте: "
            f"{len(comparison.get('court_amounts_found') or [])} из "
            f"{len(comparison.get('court_amounts') or [])}"
        )
        lines.append("")
        lines.append("### Проверка")
        issues = (item.get("verification") or {}).get("issues") or []
        lines.append(f"- замечаний: {len(issues)}")
        for issue in issues[:8]:
            lines.append(
                f"- [{issue.get('severity')}] {issue.get('type')}: "
                f"{(issue.get('text') or '')[:180]}"
            )
        if len(issues) > 8:
            lines.append(f"- … ещё {len(issues) - 8}")
        lines.append("")
        lines.append("### Проект ИИ")
        lines.append("")
        lines.append("```text")
        lines.append((item.get("draft_text") or "")[:16000])
        lines.append("```")
        lines.append("")
        lines.append("### Решение суда")
        lines.append("")
        lines.append("```text")
        lines.append((item.get("reference_text") or "")[:9000])
        lines.append("```")
        lines.append("")
        lines.append("### Что проверить судье")
        lines.append("")
        lines.append("- [ ] стороны, предмет и основание иска")
        lines.append("- [ ] суммы, пошлина и итог")
        lines.append("- [ ] применимые нормы и их редакция")
        lines.append("- [ ] оценка доказательств и бремя доказывания")
        lines.append("- [ ] резолютивная часть: кому, сколько и что взыскать")
        lines.append("")

    out = Path(args.out)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"написано: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
