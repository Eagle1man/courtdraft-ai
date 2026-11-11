#!/usr/bin/env python3
"""Синтетические учебные дела из sud-resh-benchmark.

Из одного судебного акта (7 блоков) собираем материалы дела:
иск, отзыв, протокол — без логики судьи и итогового решения.
Решение суда остаётся только в court_reference.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.guardrails import check_materials  # noqa: E402

DEFAULT_SOURCE = ROOT / "data" / "external" / "sud_resh" / "sud_resh_benchmark.json"
DEFAULT_OUT = ROOT / "artifacts" / "cd" / "synth" / "cases"
URL = (
    "https://huggingface.co/datasets/lawful-good-project/"
    "sud-resh-benchmark/resolve/main/sud_resh_benchmark.json"
)

BLOCKS = {
    "требования истца": "requirements",
    "аргументы истца": "plaintiff_arguments",
    "аргументы ответчика": "defendant_arguments",
    "оценку судом": "evidence_assessment",
    "логику рассуждений": "judge_logic",
    "применимые нормы": "norms",
    "краткое содержание окончательного решения": "decision",
}


def download(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"скачивание: {URL}")
    urllib.request.urlretrieve(URL, path)  # noqa: S310 - фиксированный URL


def block_of(instruction: str) -> str:
    low = (instruction or "").lower()
    for marker, name in BLOCKS.items():
        if marker in low:
            return name
    return ""


def material_text(blocks: dict[str, str]) -> str:
    parts = [
        "ИСКОВОЕ ЗАЯВЛЕНИЕ",
        "",
        blocks.get("requirements", ""),
        "",
        "В обоснование иска указано:",
        blocks.get("plaintiff_arguments", ""),
    ]
    return "\n".join(part for part in parts if part is not None).strip() + "\n"


def response_text(blocks: dict[str, str]) -> str:
    body = blocks.get("defendant_arguments", "").strip()
    if not body:
        body = "Возражения ответчика в материалах дела не представлены."
    return "ВОЗРАЖЕНИЯ ОТВЕТЧИКА\n\n" + body + "\n"


def protocol_text() -> str:
    return (
        "ПРОТОКОЛ СУДЕБНОГО ЗАСЕДАНИЯ\n\n"
        "Стороны поддержали заявленные доводы и возражения. "
        "Иных ходатайств и доказательств не представлено.\n"
    )


def safe_slug(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"synth-{digest}"


def valid_case(materials: str, decision: str) -> list[str]:
    problems: list[str] = []
    if len(materials.strip()) < 200:
        problems.append("слишком короткие материалы")
    if len(decision.strip()) < 20:
        problems.append("пустое решение")
    leaks = check_materials([materials])
    if leaks:
        problems.append("утечка судебного акта в материалы")
    if decision.strip() and decision.strip()[:60].lower() in materials.lower():
        problems.append("решение дословно попало в материалы")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--per-category", type=int, default=0)
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        if args.download:
            download(source)
        else:
            print(f"нет файла {source}; запустите с --download")
            return 2

    raw = json.loads(source.read_text(encoding="utf-8"))
    rows = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print("неожиданный формат датасета")
        return 2

    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"blocks": {}, "category": ""})
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_text = str(row.get("source") or "")
        if not source_text:
            continue
        key = hashlib.sha256(source_text.encode("utf-8", "replace")).hexdigest()
        item = grouped[key]
        item["source"] = source_text
        item["category"] = str(row.get("category") or "")
        block = block_of(str(row.get("instruction") or ""))
        if block:
            item["blocks"][block] = str(row.get("correct_answer") or "")

    selected: list[tuple[str, dict[str, Any]]] = []
    per_category: Counter[str] = Counter()
    limit = args.per_category or max(1, args.count // 10)
    for key, item in grouped.items():
        category = str(item.get("category") or "unknown")
        if per_category[category] >= limit:
            continue
        selected.append((key, item))
        per_category[category] += 1
        if len(selected) >= args.count:
            break

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped: list[dict[str, str]] = []
    for key, item in selected:
        blocks = item.get("blocks") or {}
        materials = material_text(blocks)
        response = response_text(blocks)
        decision = str(blocks.get("decision") or "").strip()
        problems = valid_case(materials + "\n" + response, decision)
        if problems:
            skipped.append({"id": key[:12], "reason": "; ".join(problems)})
            continue
        slug = safe_slug(item.get("source", key))
        case_dir = out / slug
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "00_case_info.txt").write_text(
            "СВЕДЕНИЯ О ДЕЛЕ\n(по материалам дела)\n\n"
            f"Категория: {item.get('category', '')}\n",
            encoding="utf-8",
        )
        (case_dir / "01_claim.txt").write_text(materials, encoding="utf-8")
        (case_dir / "02_response.txt").write_text(response, encoding="utf-8")
        (case_dir / "05_protocol.txt").write_text(protocol_text(), encoding="utf-8")
        (case_dir / "court_reference.txt").write_text(decision + "\n", encoding="utf-8")
        written += 1

    manifest = {
        "source": str(source),
        "rows": len(rows),
        "decisions": len(grouped),
        "selected": len(selected),
        "written": written,
        "skipped": skipped,
        "categories": dict(per_category),
    }
    (out.parent / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
