#!/usr/bin/env python3
"""Проверка правил резолютивной части на корпусе без вызовов модели."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.agents.draft_agent import _build_operative_section  # noqa: E402
from app.agents.evidence_pack import build_evidence_pack, load_template  # noqa: E402
from app.config import EmbeddingProviderName, Settings  # noqa: E402
from eval_draft import load_case  # noqa: E402

CASES = ROOT / "data" / "lora" / "eval_cases"
OUT = ROOT / "artifacts" / "rules-audit-2026-11-11.md"
PLACEHOLDER = re.compile(
    r"не указана сумма|не установлен размер|требования истца не установлены"
    r"|резолютивная часть не сформирована|указать\s*\(",
    re.I,
)


def main() -> int:
    settings = Settings(embedding_provider=EmbeddingProviderName.MOCK)
    template = load_template(settings)
    section = next(item for item in template["sections"] if item["key"] == "operative")
    rows: list[dict[str, object]] = []
    for case_dir in sorted(path for path in CASES.glob("case-*") if path.is_dir()):
        joined, documents, reference = load_case(case_dir)
        pack = build_evidence_pack(
            case_dir.name, "Суд", "гражданское", documents, settings=settings
        )
        draft = _build_operative_section(section, pack)
        text = draft.text
        money = []
        fee = None
        gaps = draft.gaps
        rows.append(
            {
                "case": case_dir.name,
                "money": money,
                "fee": fee,
                "gaps": gaps,
                "placeholder": bool(PLACEHOLDER.search(text)),
                "text": text,
                "reference": reference,
            }
        )

    lines = ["# Аудит правил резолютивной части", ""]
    lines.append(f"- Дел: {len(rows)}")
    lines.append(
        f"- С незаполненными местами: {sum(1 for row in rows if row['placeholder'])}"
    )
    lines.append("")
    lines.append("| Дело | Требований | Пошлина | Пробелы |")
    lines.append("|---|---:|---:|---|")
    for row in rows:
        gaps = "; ".join(str(g) for g in row["gaps"]) or "—"
        lines.append(
            f"| {row['case']} | {len(row['money'])} | {row['fee'] or '—'} | {gaps} |"
        )
    lines.append("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT.with_suffix(".json")).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"написано: {OUT}")
    print(f"placeholder: {sum(1 for row in rows if row['placeholder'])}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
