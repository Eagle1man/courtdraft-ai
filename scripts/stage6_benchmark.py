"""Этап 6. Baseline на 20 реальных парах «материалы → эталонное решение».

Что делает:
  1. Разбирает один файл с решениями на 20 решений по номерам дел.
  2. Сопоставляет с материалами (по номеру дела).
  3. Ложные срабатывания: прогоняет детерминированную проверку сумм по
     ЭТАЛОННОМУ решению. Корректный акт не должен получать замечаний.
  4. Обнаружение: подменяет одну сумму в решении и проверяет, что
     валидатор это поймал.

Это измерение на реальных текстах, а не на синтетике. Эталонные
решения в материалы дела не попадают — они только источник для замера.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.verification.deterministic import check_amounts_by_purpose
from app.drafting.numeric_guard import check_unsourced_amounts

DATA = Path(r"C:\Users\diorl\OneDrive\Рабочий стол\дело 1\20 дел и решения по ним")
CASE_RE = re.compile(r"(\d+-\d+/\d{4})")
MONEY_RE = re.compile(r"\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d{2})?")


def parse_decisions(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"(?im)^##\s+решение\s+по\s+делу\s+№\s*", text)
    decisions: dict[str, str] = {}
    for chunk in parts[1:]:
        match = CASE_RE.search(chunk)
        if not match:
            continue
        body = chunk[match.end():]
        body = body.split("\n---")[0]
        decisions[match.group(1)] = body.strip()
    return decisions


def parse_materials(folder: Path) -> dict[str, str]:
    materials: dict[str, str] = {}
    for path in folder.glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "Эталонные решения" in text[:200]:
            continue
        match = CASE_RE.search(text[:400])
        if not match:
            continue
        materials.setdefault(match.group(1), "")
        materials[match.group(1)] += "\n" + text
    return materials


def main() -> None:
    files = list(DATA.glob("*.md"))
    big = max(files, key=lambda p: p.stat().st_size)
    decisions = parse_decisions(big)
    materials = parse_materials(DATA)

    pairs = sorted(set(decisions) & set(materials))
    print(f"решений разобрано: {len(decisions)}")
    print(f"материалов найдено: {len(materials)}")
    print(f"пар: {len(pairs)}")

    dataset = [
        {"case": case, "materials": materials[case].strip(), "decision": decisions[case]}
        for case in pairs
    ]
    out = ROOT / "artifacts" / "stage6_dataset.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"датасет: {out}")

    false_positive = 0
    detected = 0
    injected_total = 0
    per_case: list[str] = []

    for row in dataset:
        decision, source = row["decision"], row["materials"]
        # В рабочем конвейере суммы проверяются двумя правилами:
        # назначением (узко) и наличием любой суммы в материалах (широко).
        # Мерить надо то, что реально включено, иначе цифра занижена.
        purpose_findings = check_amounts_by_purpose(decision, [source])
        unsourced = check_unsourced_amounts(decision, [source])
        if purpose_findings or unsourced:
            false_positive += len(purpose_findings) + len(unsourced)
            per_case.append(
                f"{row['case']}: FP purpose={[f.check_id for f in purpose_findings]} "
                f"unsourced={[u.raw for u in unsourced]}"
            )

        amounts = MONEY_RE.findall(decision)
        if not amounts:
            continue
        original = amounts[0]
        broken = original.replace(original[-3:], "999") if len(original) > 3 else "999 999"
        mutated = decision.replace(original, broken, 1)
        injected_total += 1
        if check_amounts_by_purpose(mutated, [source]) or check_unsourced_amounts(
            mutated, [source]
        ):
            detected += 1
        else:
            per_case.append(f"{row['case']}: подмена не поймана")

    print("\n=== ЛОЖНЫЕ СРАБАТЫВАНИЯ НА ЭТАЛОННЫХ АКТАХ ===")
    print(f"пар: {len(dataset)}, срабатываний: {false_positive}")
    print("\n=== ОБНАРУЖЕНИЕ ПОДМЕНЁННОЙ СУММЫ ===")
    print(f"поймано: {detected}/{injected_total}")
    if per_case:
        print("\nдетали:")
        for line in per_case:
            print(" ", line)

    summary = {
        "pairs": len(dataset),
        "false_positive_findings": false_positive,
        "injected": injected_total,
        "detected": detected,
    }
    (ROOT / "docs" / "stage6-baseline.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nсохранено: docs/stage6-baseline.json")


if __name__ == "__main__":
    main()
