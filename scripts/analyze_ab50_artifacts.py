#!/usr/bin/env python3
"""Разбор артефактов A/B без повторных вызовов модели.

Что считает:

* P0 по типам и разделам — как записано в артефакте и после снятия
  ложных ``UNSUPPORTED_CLAIM``, где все числа оказались частями дат
  (``24.03.2016`` — ссылка на Пленум, а не выдуманная сумма);
* citation F1 по полному тексту решения: ``06_court_findings.txt`` +
  резолюция, а не одна резолюция (в ней статей почти нет);
* суммы: recall/precision/F1 по уникальным целым рублям и дела, где
  суммы суда подтверждены не полностью.

Проверка не перезапускается: берутся сохранённые ``verification.issues``
и ``draft_text``. Поэтому дата-фильтр — это оценка эффекта исправления
на уже собранных данных, а не новый прогон верификатора. Сам верификатор
покрыт тестами отдельно.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\analyze_ab50_artifacts.py
    ... --run graphrag=artifacts/graphrag50-2026-11-12
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import eval_harness as eh  # noqa: E402
from app.verification.engine import is_date_only_number  # noqa: E402

DEFAULT_CASES = ROOT / "data" / "lora" / "eval_cases"
DEFAULT_RUNS = {
    "baseline": ROOT / "artifacts" / "ab50-baseline",
    "pipeline": ROOT / "artifacts" / "ab50-pipeline",
}
DEFAULT_OUT = ROOT / "artifacts" / "ab50-analysis-2026-11-12"

DATE_NUMBER_RE = re.compile(r"число\s+(\d+)", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(
    r"не указана сумма|не установлен размер|требования истца не установлены"
    r"|резолютивная часть не сформирована|указать\s*\(",
    re.IGNORECASE,
)

TYPE_LABELS = {
    "UNSUPPORTED_CLAIM": "неподтверждённое число/факт",
    "CONTRADICTION": "противоречие",
    "MONEY_MISMATCH": "несовпадение суммы",
    "LAW_NOT_FOUND": "норма без источника",
    "INCOMPLETE": "неполный раздел",
    "CITATION_MISSING": "нет ссылки на источник",
    "FACT_MISSING": "факт без источника",
}


def type_label(value: str) -> str:
    label = TYPE_LABELS.get(value)
    return f"{value} — {label}" if label else value


def load_run(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for file in sorted((path / "cases").glob("*.json")):
        cases.append(json.loads(file.read_text(encoding="utf-8")))
    return cases


def load_references(cases_root: Path, case_name: str) -> tuple[str, str]:
    """Резолюция и полный текст решения для сверки.

    Полный текст = установленные судом обстоятельства + резолюция.
    Резолюция остаётся отдельной: по ней считаются исход и суммы.
    """
    case_dir = cases_root / case_name
    reference_path = case_dir / "court_reference.txt"
    reference = (
        reference_path.read_text(encoding="utf-8").strip()
        if reference_path.exists()
        else ""
    )
    findings_path = case_dir / "06_court_findings.txt"
    if not findings_path.exists():
        return reference, reference
    findings = findings_path.read_text(encoding="utf-8").strip()
    return reference, f"{findings}\n\n{reference}".strip()


def date_only_issue(issue: dict[str, Any], draft_text: str) -> bool:
    """True, если все числа в замечании — части дат в тексте проекта."""
    if issue.get("type") != "UNSUPPORTED_CLAIM":
        return False
    numbers = DATE_NUMBER_RE.findall(issue.get("reason") or "")
    if not numbers:
        return False
    return all(is_date_only_number(draft_text, number) for number in numbers)


def recompute_p0(
    case: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """P0 после снятия дата-ложных замечаний.

    Некрит­ические причины (пустой раздел, незаполненная резолюция)
    берутся из артефакта как есть: они не зависят от проверки чисел.
    Критические пересобираются из отфильтрованных issues.
    """
    draft_text = case.get("draft_text") or ""
    issues = list((case.get("verification") or {}).get("issues") or [])
    kept_issues: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    seen_issues: set[tuple[str, str, str]] = set()
    for issue in issues:
        if issue.get("severity") != "CRITICAL":
            continue
        if date_only_issue(issue, draft_text):
            removed.append(issue)
            continue
        key = (
            issue.get("type") or "",
            (issue.get("text") or "")[:120],
            issue.get("section") or "",
        )
        if key in seen_issues:
            continue
        seen_issues.add(key)
        kept_issues.append(issue)

    kept: list[str] = [
        reason
        for reason in (case.get("p0") or [])
        if not reason.startswith("CRITICAL ")
    ]
    seen: set[tuple[str, str]] = set()
    for issue in kept_issues:
        key = (issue.get("type") or "", (issue.get("text") or "")[:120])
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            f"CRITICAL {issue.get('type')}: {(issue.get('text') or '')[:120]}"
        )
    return kept, removed, kept_issues


def p0_by_type_section(
    kept_issues: list[dict[str, Any]],
) -> Counter[tuple[str, str]]:
    counter: Counter[tuple[str, str]] = Counter()
    for issue in kept_issues:
        counter[
            (
                type_label(issue.get("type") or "?"),
                issue.get("section") or "проект целиком",
            )
        ] += 1
    return counter


def citation_metrics(case: dict[str, Any], full_reference: str) -> dict[str, Any]:
    system = eh.laws_in(case.get("draft_text") or "")
    court = eh.laws_in(full_reference)
    both = system.keys() & court.keys()
    precision = len(both) / len(system) if system else 0.0
    recall = len(both) / len(court) if court else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "system_count": len(system),
        "court_count": len(court),
        "both_count": len(both),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "system_only": sorted(system[key] for key in system.keys() - court.keys()),
        "court_only": sorted(court[key] for key in court.keys() - system.keys()),
    }


def amount_metrics(
    case: dict[str, Any], reference: str
) -> dict[str, Any]:
    court = eh.rub_amounts(reference)
    system = eh.rub_amounts(case.get("operative_text") or "")
    metrics = eh.amount_metrics(court, system)
    return {
        **metrics,
        "missing": sorted(set(metrics["court_unique"]) - set(metrics["found"])),
        "extra": sorted(set(metrics["system_unique"]) - set(metrics["found"])),
    }


def aggregate_citation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    system = sum(row["citation"]["system_count"] for row in rows)
    court = sum(row["citation"]["court_count"] for row in rows)
    both = sum(row["citation"]["both_count"] for row in rows)
    precision = both / system if system else 0.0
    recall = both / court if court else 0.0
    f1 = 2 * both / (system + court) if system + court else 0.0
    scored = [row["citation"]["f1"] for row in rows if row["citation"]["court_count"]]
    return {
        "micro_precision": round(precision, 4),
        "micro_recall": round(recall, 4),
        "micro_f1": round(f1, 4),
        "macro_f1": round(sum(scored) / len(scored), 4) if scored else 0.0,
        "scored_cases": len(scored),
        "system_laws": system,
        "court_laws": court,
    }


def aggregate_amounts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    court = sum(row["amounts"]["court_count"] for row in rows)
    system = sum(row["amounts"]["system_count"] for row in rows)
    found = sum(row["amounts"]["found_count"] for row in rows)
    precision = found / system if system else 0.0
    recall = found / court if court else 0.0
    f1 = 2 * found / (system + court) if system + court else 0.0
    return {
        "court_unique": court,
        "system_unique": system,
        "found": found,
        "micro_precision": round(precision, 4),
        "micro_recall": round(recall, 4),
        "micro_f1": round(f1, 4),
        "full_cases": sum(1 for row in rows if row["amounts"]["full_coverage"]),
    }


def analyze_run(name: str, path: Path, cases_root: Path) -> dict[str, Any]:
    cases = load_run(path)
    rows: list[dict[str, Any]] = []
    by_type_section: Counter[tuple[str, str]] = Counter()
    removed_numbers: Counter[str] = Counter()
    removed_cases: list[str] = []
    for case in cases:
        case_name = case.get("case") or "?"
        reference, full_reference = load_references(cases_root, case_name)
        kept, removed, kept_issues = recompute_p0(case)
        if removed:
            removed_cases.append(case_name)
            for issue in removed:
                removed_numbers.update(
                    DATE_NUMBER_RE.findall(issue.get("reason") or "")
                )
        by_type_section.update(p0_by_type_section(kept_issues))
        rows.append(
            {
                "case": case_name,
                "error": case.get("error") or "",
                "cost_rub": float(case.get("cost_rub") or 0.0),
                "p0_stored": list(case.get("p0") or []),
                "p0_clean": kept,
                "removed_date_issues": [
                    {
                        "type": issue.get("type"),
                        "section": issue.get("section"),
                        "reason": issue.get("reason"),
                    }
                    for issue in removed
                ],
                "critical_by_type_section": [
                    {"type": type_label(issue.get("type") or "?"),
                     "section": issue.get("section") or "проект целиком"}
                    for issue in kept_issues
                ],
                "citation": citation_metrics(case, full_reference),
                "amounts": amount_metrics(case, reference),
            }
        )
    return {
        "path": str(path),
        "cases": len(cases),
        "errors": sum(1 for row in rows if row["error"]),
        "cost_rub": round(sum(row["cost_rub"] for row in rows), 4),
        "p0_stored_cases": sum(1 for row in rows if row["p0_stored"]),
        "p0_clean_cases": sum(1 for row in rows if row["p0_clean"]),
        "removed_issues": sum(len(row["removed_date_issues"]) for row in rows),
        "removed_cases": removed_cases,
        "removed_numbers": dict(removed_numbers),
        "by_type_section": {
            f"{kind} | {section}": count
            for (kind, section), count in by_type_section.most_common()
        },
        "citation": aggregate_citation(rows),
        "amounts": aggregate_amounts(rows),
        "rows": rows,
    }


def fmt_rub(value: float) -> str:
    return f"{value:.4f}"


def write_markdown(runs: dict[str, dict[str, Any]], out: Path) -> None:
    lines: list[str] = []
    lines.append("# Разбор артефактов A/B: P0, citation F1 и суммы")
    lines.append("")
    lines.append(f"- Дата разбора: {datetime.now():%Y-%m-%d %H:%M}")
    lines.append("- Вызовов модели: 0 — только сохранённые артефакты")
    lines.append(
        "- P0 пересчитан по `verification.issues`; сняты только "
        "`UNSUPPORTED_CLAIM`, где все числа — части дат "
        "(`24.03.2016`, `24 марта 2016 года`). Верификатор заново "
        "не запускался."
    )
    lines.append(
        "- Citation F1: проект против `06_court_findings.txt` + резолюция. "
        "Резолюция одна даёт 3 упоминания статей на 50 дел — F1 по ней "
        "измерял не качество, а пустоту эталона."
    )
    lines.append(
        "- Суммы: уникальные целые рубли; копейки в значение не входят, "
        "итог с расшифровкой в скобках учитывается."
    )
    lines.append("")

    lines.append("## Сводка")
    lines.append("")
    lines.append(
        "| Прогон | Дел | Ошибок | P0 как записано | P0 после снятия дат | "
        "Снято дата-замечаний | Citation micro F1 | Amount micro F1 | "
        "Amount recall | Суммы полностью | ₽ |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, run in runs.items():
        lines.append(
            f"| {name} | {run['cases']} | {run['errors']} | "
            f"{run['p0_stored_cases']} | {run['p0_clean_cases']} | "
            f"{run['removed_issues']} | {run['citation']['micro_f1']:.4f} | "
            f"{run['amounts']['micro_f1']:.4f} | "
            f"{run['amounts']['micro_recall']:.4f} | "
            f"{run['amounts']['full_cases']}/{run['cases']} | "
            f"{fmt_rub(run['cost_rub'])} |"
        )
    lines.append("")

    lines.append("## P0 по типам и разделам (после снятия дат)")
    lines.append("")
    keys: list[str] = []
    for run in runs.values():
        for key in run["by_type_section"]:
            if key not in keys:
                keys.append(key)
    if keys:
        lines.append("| Тип | Раздел | " + " | ".join(runs) + " |")
        lines.append("|---|---|" + "---:|" * len(runs))
        for key in keys:
            kind, section = key.split(" | ", 1)
            cells = [
                str(run["by_type_section"].get(key, 0)) for run in runs.values()
            ]
            lines.append(f"| {kind} | {section} | " + " | ".join(cells) + " |")
    else:
        lines.append("- Критических замечаний не осталось.")
    lines.append("")

    lines.append("## Снятые ложные P0 (даты)")
    lines.append("")
    for name, run in runs.items():
        numbers = ", ".join(
            f"{number}×{count}" for number, count in run["removed_numbers"].items()
        )
        lines.append(
            f"- **{name}**: {run['removed_issues']} замечаний в "
            f"{len(run['removed_cases'])} делах"
            + (f"; числа: {numbers}" if numbers else "")
        )
    lines.append("")

    lines.append("## Оставшиеся P0")
    lines.append("")
    for name, run in runs.items():
        lines.append(f"### {name}")
        lines.append("")
        left = [row for row in run["rows"] if row["p0_clean"]]
        if not left:
            lines.append("- нет")
        for row in left:
            reasons = "; ".join(row["p0_clean"])
            lines.append(f"- **{row['case']}**: {reasons}")
        lines.append("")

    lines.append("## Citation и суммы по делам: худшие")
    lines.append("")
    for name, run in runs.items():
        lines.append(f"### {name}")
        lines.append("")
        worst = sorted(
            run["rows"],
            key=lambda row: (row["citation"]["f1"], row["amounts"]["f1"]),
        )[:5]
        lines.append("| Дело | Citation F1 | Норм у суда | Норм только у суда | Amount F1 | Не подтверждены |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for row in worst:
            missing = ", ".join(str(value) for value in row["amounts"]["missing"])
            lines.append(
                f"| {row['case']} | {row['citation']['f1']:.4f} | "
                f"{row['citation']['court_count']} | "
                f"{len(row['citation']['court_only'])} | "
                f"{row['amounts']['f1']:.4f} | {missing or '—'} |"
            )
        lines.append("")

    lines.append("## Файлы")
    lines.append("")
    lines.append(f"- Markdown: `{out.with_suffix('.md')}`")
    lines.append(f"- JSON: `{out.with_suffix('.json')}`")
    lines.append("")
    out.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def parse_run(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("ожидается имя=путь")
    name, raw = value.split("=", 1)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return name, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        type=parse_run,
        default=None,
        help="имя=путь к каталогу артефактов; можно несколько раз",
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    runs_spec = args.run or list(DEFAULT_RUNS.items())
    runs: dict[str, dict[str, Any]] = {}
    for name, path in runs_spec:
        if not (path / "cases").exists():
            print(f"пропуск {name}: нет {path / 'cases'}")
            continue
        runs[name] = analyze_run(name, path, Path(args.cases))
        run = runs[name]
        print(
            f"{name}: дел {run['cases']}, P0 {run['p0_stored_cases']} → "
            f"{run['p0_clean_cases']}, снято {run['removed_issues']}, "
            f"citation F1 {run['citation']['micro_f1']:.4f}, "
            f"amount F1 {run['amounts']['micro_f1']:.4f}"
        )
    if not runs:
        print("Нет артефактов для разбора.")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": {
            "p0": "stored issues; date-only UNSUPPORTED_CLAIM removed",
            "citation": "draft_text vs 06_court_findings.txt + court_reference.txt",
            "amounts": "unique integer rubles from operative vs reference",
        },
        "runs": runs,
    }
    out.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(runs, out)
    print(f"написано: {out.with_suffix('.md')}")
    print(f"написано: {out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
