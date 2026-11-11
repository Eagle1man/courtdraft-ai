#!/usr/bin/env python3
"""Разбор уже собранного отчёта DeepSeek-пилота без повторных API-вызовов."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_harness as eh

PLACEHOLDER_RE = re.compile(
    r"не указана сумма|не установлен размер|требования истца не установлены"
    r"|резолютивная часть не сформирована|указать\s*\(",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    return (text or "").replace("ё", "е").replace("Ё", "Е")


def outcome_of(text: str) -> list[str]:
    lowered = normalize(text).lower()
    found: list[str] = []
    for root, label in eh.OUTCOME_MARKERS:
        if normalize(root) in lowered and label not in found:
            found.append(label)
    return found


def issue_list(case: dict[str, Any]) -> list[dict[str, Any]]:
    return list((case.get("verification") or {}).get("issues") or [])


def p0_categories(case: dict[str, Any]) -> list[str]:
    cats: list[str] = []
    for reason in case.get("p0") or []:
        if reason.startswith("CRITICAL UNSUPPORTED_CLAIM"):
            cats.append("неподтверждённое число/факт")
        elif reason.startswith("CRITICAL CONTRADICTION"):
            cats.append("противоречие")
        elif "незаполненные места" in reason or "резолютивная часть пуста" in reason:
            cats.append("резолютивная не сформирована")
        elif "пустой раздел" in reason:
            cats.append("пустой раздел")
        elif "MOCK" in reason:
            cats.append("MOCK")
        else:
            cats.append(reason[:80])
    if PLACEHOLDER_RE.search(
        case.get("operative_text") or ""
    ) and "резолютивная не сформирована" not in cats:
        cats.append("резолютивная не сформирована")
    return cats


def flagged_numbers(case: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for issue in issue_list(case):
        if issue.get("type") != "UNSUPPORTED_CLAIM":
            continue
        match = re.search(r"число (\d+)", issue.get("reason") or "", re.IGNORECASE)
        if match:
            found.append(match.group(1))
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", default=str(ROOT / "artifacts" / "deepseek-quality-2026-11-11" / "report.json"))
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    path = Path(args.report)
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    out = Path(args.out) if args.out else path.with_name("analysis.md")

    cat_counter: Counter[str] = Counter()
    number_counter: Counter[str] = Counter()
    law_counter: Counter[str] = Counter()
    p0_cases: list[str] = []
    placeholder_cases: list[str] = []
    no_p0: list[str] = []
    model_sections = 0
    empty_model_sections = 0
    outcome_cases = 0
    outcome_same = 0
    outcome_partial = 0
    outcome_diff = 0

    rows: list[dict[str, Any]] = []
    for case in cases:
        name = case.get("case", "?")
        cats = p0_categories(case)
        if cats:
            p0_cases.append(name)
        else:
            no_p0.append(name)
        cat_counter.update(cats)
        placeholder = bool(PLACEHOLDER_RE.search(case.get("operative_text") or ""))
        if placeholder:
            placeholder_cases.append(name)
        for number in flagged_numbers(case):
            number_counter[number] += 1
        for issue in issue_list(case):
            if issue.get("type") == "LAW_NOT_FOUND":
                text = issue.get("text") or ""
                for match in re.finditer(r"ст\.\s*(\d+(?:\.\d+)*)\s*([А-ЯЁа-яё]+)", text):
                    law_counter[f"ст. {match.group(1)} {match.group(2)}"] += 1
        for section in case.get("sections") or []:
            if section.get("built_by") == "model":
                model_sections += 1
                if not section.get("chars"):
                    empty_model_sections += 1

        sys_out = outcome_of(case.get("operative_text") or "")
        court_out = outcome_of(case.get("reference_text") or "")
        match_state = "—"
        if court_out:
            outcome_cases += 1
            if sorted(sys_out) == sorted(court_out):
                outcome_same += 1
                match_state = "совпал"
            elif set(sys_out) & set(court_out):
                outcome_partial += 1
                match_state = "частично"
            else:
                outcome_diff += 1
                match_state = "разошёлся"
        rows.append(
            {
                "case": name,
                "p0": cats,
                "placeholder": placeholder,
                "outcome_system": sys_out,
                "outcome_court": court_out,
                "outcome_state": match_state,
                "critical": sum(
                    1 for issue in issue_list(case) if issue.get("severity") == "CRITICAL"
                ),
                "high": sum(
                    1 for issue in issue_list(case) if issue.get("severity") == "HIGH"
                ),
                "cost": case.get("cost_rub", 0.0),
            }
        )

    lines: list[str] = []
    lines.append("# Разбор пилота DeepSeek Flash")
    lines.append("")
    lines.append(f"- Дел: {len(cases)}")
    lines.append(f"- Израсходовано: {float(data.get('spent_rub') or 0):.4f} ₽")
    lines.append(f"- Дел с P0 (после проверки резолютивной): {len(p0_cases)}")
    lines.append(f"- Дел без P0: {len(no_p0)}")
    lines.append(f"- Дел с незаполненными местами в резолютивной: {len(placeholder_cases)}")
    lines.append("")
    lines.append("## Причины P0")
    lines.append("")
    for name, count in cat_counter.most_common():
        lines.append(f"- {name}: {count}")
    lines.append("")
    lines.append("## Часто выдуманные числа (по версии проверки)")
    lines.append("")
    for number, count in number_counter.most_common(20):
        lines.append(f"- {number}: {count} раз")
    if not number_counter:
        lines.append("- нет")
    lines.append("")
    lines.append("## Нормы, которых нет в реестре (LAW_NOT_FOUND)")
    lines.append("")
    for law, count in law_counter.most_common(20):
        lines.append(f"- {law}: {count} раз")
    if not law_counter:
        lines.append("- нет")
    lines.append("")
    lines.append("## Сверка исхода с судом (там, где суд явно его записал)")
    lines.append("")
    lines.append(f"- дел с явным исходом суда: {outcome_cases}")
    lines.append(f"- совпал: {outcome_same}")
    lines.append(f"- частично: {outcome_partial}")
    lines.append(f"- разошёлся: {outcome_diff}")
    lines.append("")
    lines.append("## Модельные разделы")
    lines.append("")
    lines.append(f"- всего разделов от модели: {model_sections}")
    lines.append(f"- пустых разделов от модели: {empty_model_sections}")
    lines.append("")
    lines.append("## Дела без P0 (кандидаты на ручной разбор)")
    lines.append("")
    for name in no_p0:
        row = next(item for item in rows if item["case"] == name)
        lines.append(
            f"- {name}: исход ИИ {row['outcome_system'] or '—'}, "
            f"суд {row['outcome_court'] or '—'} ({row['outcome_state']}), "
            f"CRITICAL/HIGH {row['critical']}/{row['high']}"
        )
    lines.append("")
    lines.append("## Дела с незаполненной резолютивной")
    lines.append("")
    lines.append("- " + ", ".join(placeholder_cases) if placeholder_cases else "- нет")
    lines.append("")
    lines.append("## Таблица")
    lines.append("")
    lines.append("| Дело | P0 | Резолютивная | Исход ИИ / суд | CRITICAL/HIGH | ₽ |")
    lines.append("|---|---|---|---|---:|---:|")
    for row in rows:
        lines.append(
            f"| {row['case']} | {'; '.join(row['p0']) or '—'} | "
            f"{'не заполнена' if row['placeholder'] else '—'} | "
            f"{', '.join(row['outcome_system']) or '—'} / {', '.join(row['outcome_court']) or '—'} | "
            f"{row['critical']}/{row['high']} | {row['cost']:.4f} |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"написано: {out}")
    print(f"P0: {len(p0_cases)}/{len(cases)}; без P0: {len(no_p0)}; placeholder: {len(placeholder_cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
