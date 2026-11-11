#!/usr/bin/env python3
"""Разбор flagged-чисел: материалы / подсказка шаблона / эталон / реально нет.

Нужен, потому что проверка `verify_draft` считает любое число вне
`pack.amounts` выдуманным. Копейки («55» из «5 819 руб. 55 коп.») и дата
Пленума из подсказки шаблона («2016») попадают туда же, хотя это не
выдумка модели. Скрипт не меняет вердикт проверки, а показывает состав.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_draft import load_case

CASES_DIR = ROOT / "data" / "lora" / "eval_cases"
REPORT = ROOT / "artifacts" / "deepseek-quality-2026-11-11" / "report.json"
TEMPLATE = ROOT / "data" / "templates" / "civil_judgment.json"
OUT = REPORT.with_name("grounding-audit.md")


def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def template_text() -> str:
    raw = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    def walk(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            result: list[str] = []
            for item in value.values():
                result.extend(walk(item))
            return result
        if isinstance(value, list):
            result = []
            for item in value:
                result.extend(walk(item))
            return result
        return []

    return "\n".join(walk(raw))


def main() -> int:
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    prompt_digits = digits(template_text())
    categories: Counter[str] = Counter()
    per_case: list[dict[str, Any]] = []
    true_numbers: Counter[str] = Counter()

    for case in data.get("cases") or []:
        name = case.get("case", "")
        case_dir = CASES_DIR / name
        if not case_dir.exists():
            continue
        joined, _documents, reference = load_case(case_dir)
        material_digits = digits(joined)
        reference_digits = digits(reference)
        case_true: list[str] = []
        case_prompt: list[str] = []
        case_material: list[str] = []
        case_reference: list[str] = []

        for issue in (case.get("verification") or {}).get("issues") or []:
            if issue.get("type") != "UNSUPPORTED_CLAIM":
                continue
            for number in re.findall(r"число (\d+)", issue.get("reason") or "", re.IGNORECASE):
                if number in material_digits:
                    categories["в материалах (совпадение по цифрам)"] += 1
                    case_material.append(number)
                elif number in prompt_digits:
                    categories["в подсказке шаблона (Пленум и т.п.)"] += 1
                    case_prompt.append(number)
                elif number in reference_digits:
                    categories["в эталоне суда (модели не отправлялся)"] += 1
                    case_reference.append(number)
                else:
                    categories["нет в материалах — реально не подтверждено"] += 1
                    true_numbers[number] += 1
                    case_true.append(number)

        per_case.append(
            {
                "case": name,
                "material": sorted(set(case_material)),
                "prompt": sorted(set(case_prompt)),
                "reference": sorted(set(case_reference)),
                "true": sorted(set(case_true)),
            }
        )

    lines: list[str] = []
    lines.append("# Аудит происхождения чисел, помеченных как выдуманные")
    lines.append("")
    lines.append("| Категория | Сколько раз |")
    lines.append("|---|---:|")
    for name, count in categories.most_common():
        lines.append(f"| {name} | {count} |")
    lines.append("")
    lines.append("## Числа, реально отсутствующие в материалах")
    lines.append("")
    for number, count in true_numbers.most_common(30):
        lines.append(f"- {number}: {count} раз")
    if not true_numbers:
        lines.append("- нет")
    lines.append("")
    lines.append("## По делам")
    lines.append("")
    lines.append("| Дело | В материалах | В подсказке | В эталоне | Реально нет |")
    lines.append("|---|---|---|---|---|")
    for item in per_case:
        if not (item["material"] or item["prompt"] or item["reference"] or item["true"]):
            continue
        lines.append(
            f"| {item['case']} | {', '.join(item['material']) or '—'} | "
            f"{', '.join(item['prompt']) or '—'} | {', '.join(item['reference']) or '—'} | "
            f"{', '.join(item['true']) or '—'} |"
        )
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"написано: {OUT}")
    for name, count in categories.most_common():
        print(f"{name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
