#!/usr/bin/env python3
r"""Перепроверить сохранённые артефакты A/B без вызовов модели.

Пересобирает EvidencePack из материалов дела и DraftResult из
сохранённого текста, заново запускает verify_draft/p0_reasons.
Нужно после исправлений в верификаторе, когда офлайн-фильтр
(даты/ссылки в UNSUPPORTED_CLAIM) не покрывает другие типы
(например CONTRADICTION ролей).

Запуск:
    backend\.venv\Scripts\python.exe scripts\reverify_artifacts.py
    ... --run pipeline=artifacts/ab50-pipeline
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.draft_agent import DraftResult, SectionDraft  # noqa: E402
from app.agents.evidence_pack import build_evidence_pack  # noqa: E402
from app.config import Settings  # noqa: E402
from app.verification.engine import verify_draft  # noqa: E402
from eval_deepseek_quality import (  # noqa: E402
    compare_with_reference,
    load_citation_reference,
    operative_text_of,
    p0_reasons,
    p1_reasons,
)
from eval_draft import load_case  # noqa: E402

DEFAULT_RUNS = {
    "baseline": ROOT / "artifacts" / "ab50-baseline",
    "pipeline": ROOT / "artifacts" / "ab50-pipeline",
    "graphrag": ROOT / "artifacts" / "graphrag50-2026-11-12",
}


def split_sections(draft_text: str, meta: list[dict[str, Any]]) -> list[SectionDraft]:
    text = draft_text or ""
    positions: list[tuple[int, dict[str, Any]]] = []
    for section in meta:
        name = (section.get("name") or "").strip()
        if not name:
            continue
        marker = f"{name.upper()}\n\n"
        idx = text.find(marker)
        if idx < 0:
            marker = f"{name.upper()}\n"
            idx = text.find(marker)
        if idx >= 0:
            positions.append((idx, section))
    positions.sort()
    sections: list[SectionDraft] = []
    for i, (start, section) in enumerate(positions):
        name = (section.get("name") or "").strip()
        marker = f"{name.upper()}\n\n"
        body_start = start + len(marker)
        if not text.startswith(marker, start):
            marker = f"{name.upper()}\n"
            body_start = start + len(marker)
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[body_start:end].strip()
        sections.append(
            SectionDraft(
                key=section.get("key") or "",
                name=name,
                text=body,
                is_mock=False,
                citations=[],
                needs_review=bool(section.get("needs_review")),
                latency_ms=0,
                error=section.get("error") or "",
                built_by=section.get("built_by") or "model",
            )
        )
    if not sections:
        # fallback: whole text as one section
        for section in meta:
            sections.append(
                SectionDraft(
                    key=section.get("key") or "",
                    name=section.get("name") or "",
                    text="",
                    built_by=section.get("built_by") or "model",
                )
            )
    return sections


def reverify_case(path: Path, cases_root: Path, settings: Settings) -> dict[str, Any] | None:
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("error"):
        return None
    case = d.get("case")
    case_dir = cases_root / case
    if not case_dir.exists():
        return None
    label, docs, reference = load_case(case_dir)
    pack = build_evidence_pack(
        label,
        "Суд отложенного замера",
        "договорные споры",
        docs,
        settings=settings,
    )
    sections = split_sections(d.get("draft_text") or "", d.get("sections") or [])
    draft = DraftResult(
        case_number=label,
        provider=d.get("provider") or "",
        model=d.get("model") or "",
        sections=sections,
    )
    report = verify_draft(draft, pack)
    operative = operative_text_of(draft) or d.get("operative_text") or ""
    comparison = compare_with_reference(
        draft,
        operative,
        reference,
        load_citation_reference(case_dir, reference),
    )
    severe = sum(
        1
        for issue in report.issues
        if getattr(issue.severity, "value", issue.severity) in ("HIGH", "CRITICAL")
    )
    d["verification"] = {
        "status": report.status.value,
        "claims_checked": report.claims_checked,
        "unsupported_claims": report.unsupported_claims,
        "severe_issues": severe,
        "issues": [issue.to_dict() for issue in report.issues],
    }
    d["comparison"] = comparison
    d["p0"] = p0_reasons(draft, report, operative)
    d["p1"] = p1_reasons(report)
    d["outcome_system"] = comparison["outcome_system"]
    d["outcome_court"] = comparison["outcome_court"]
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"case": case, "p0": len(d["p0"]), "severe": severe}


def parse_run(value: str) -> tuple[str, Path]:
    name, raw = value.split("=", 1)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return name, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", type=parse_run)
    parser.add_argument("--cases", default=str(ROOT / "data" / "lora" / "eval_cases"))
    args = parser.parse_args(argv)
    runs = args.run or list(DEFAULT_RUNS.items())
    cases_root = Path(args.cases)
    settings = Settings()
    for name, path in runs:
        case_dir = path / "cases"
        if not case_dir.exists():
            print(f"пропуск {name}: нет {case_dir}")
            continue
        total = p0_cases = severe = 0
        for f in sorted(case_dir.glob("*.json")):
            res = reverify_case(f, cases_root, settings)
            if not res:
                continue
            total += 1
            severe += res["severe"]
            if res["p0"]:
                p0_cases += 1
        print(f"{name}: дел {total}, P0 {p0_cases}, тяжёлых {severe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
