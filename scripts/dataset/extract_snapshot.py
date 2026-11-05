"""SFT-пары из снимка стенда: решения суда, которые были в материалах теста.

Зачем отдельный сборщик: корпусный набор — это чужие решения из выгрузки
судов, а здесь те же формулировки и те же шаблоны, с которыми работает
программа на стенде. Разница в стиле для маленькой модели заметна.

Что берём: документы, в которых есть и «УСТАНОВИЛ», и «РЕШИЛ» — это
настоящие решения, а не проекты, сгенерированные моделью. Проекты в
обучение не идут: в них есть утверждения без источника, и учить на них
значило бы закреплять ошибки.

Запуск:
    python scripts\\dataset\\extract_snapshot.py --dry-run
    python scripts\\dataset\\extract_snapshot.py \\
        --runs D:\\courtdraft-handoff\\handoff-...\\courtdraft.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sqlite3
import sys

import docx
import pymupdf

from app.agents.draft_agent import (
    NEEDS_REVIEW,
    NO_LAW_FOUND,
    SYSTEM_PROMPT,
    build_section_prompt,
)
from app.agents.evidence_pack import EvidenceItem, EvidencePack, load_template
from app.datasets.court_decisions import clean_decision, split_sections
from app.drafting.date_guard import extract_dates
from app.drafting.numeric_guard import extract_money

sys.stdout.reconfigure(errors="replace")

DEFAULT_SNAPSHOT = pathlib.Path(r"D:\courtdraft-handoff\handoff-20261105-1713")
DEFAULT_CORPUS = pathlib.Path(r"D:\courtdraft-datasets\sft-established-feb2026.jsonl")
DEFAULT_OUTPUT = pathlib.Path(r"D:\courtdraft-datasets\sft-established-mixed.jsonl")

DECIDED = re.compile(r"Р\s*Е\s*Ш\s*И\s*Л", re.IGNORECASE)
ESTABLISHED = re.compile(r"У\s*С\s*Т\s*А\s*Н\s*О\s*В\s*И\s*Л", re.IGNORECASE)

MIN_MATERIALS = 200
MIN_TARGET = 300


def read_text(path: pathlib.Path) -> str:
    """Текст документа из любого формата, который встречается в тесте."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            document = docx.Document(str(path))
            parts = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            return "\n".join(parts)
        if suffix == ".pdf":
            with pymupdf.open(str(path)) as document:
                return "\n".join(page.get_text() for page in document)
        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"  не прочитан {path.name}: {exc}")
    return ""


def is_decision(text: str) -> bool:
    """Решение ли это: оба заголовка в тексте, а не только слово «решение»."""
    return bool(DECIDED.search(text)) and bool(ESTABLISHED.search(text))


def has_anonymisation(text: str) -> bool:
    return "<" in text or ">" in text


def split_of(case_id: str) -> str:
    digest = hashlib.sha256(case_id.encode()).hexdigest()
    return "val" if int(digest[:8], 16) % 20 == 0 else "train"


def pack_for(record: dict, materials: str) -> EvidencePack:
    """Пакет доказательств из материалов дела этого же дела."""
    pack = EvidencePack(
        case_number=record.get("case_number", ""),
        court_name=record.get("court_name", ""),
        category=record.get("category", ""),
        jurisdiction="arbitration" if record.get("case_type") == "arbitration" else "civil",
    )
    pack.documents["материалы дела.txt"] = materials
    pack.items.append(
        EvidenceItem(
            category="claims",
            text=materials[:6000],
            source_document="материалы дела.txt",
            source_type="CASE_MATERIALS",
            order=1,
        )
    )
    joined = f"{materials}\n{record.get('target', '')}"
    pack.amounts = [raw for raw, _ in extract_money(joined)]
    pack.dates = [raw for raw, _ in extract_dates(joined)]
    return pack


def collect_cases(root: pathlib.Path, db: pathlib.Path) -> list[dict]:
    """Разложить документы снимка по делам."""
    meta: dict[str, dict] = {}
    if db.exists():
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            "select id, case_number, court_name, category, case_type from cases"
        ):
            meta[row["id"]] = dict(row)
        connection.close()

    cases: list[dict] = []
    originals = root / "cases" / "originals"
    if not originals.exists():
        raise SystemExit(f"нет каталога документов: {originals}")

    for case_dir in sorted(originals.iterdir()):
        if not case_dir.is_dir():
            continue
        decisions: list[str] = []
        materials: list[str] = []
        for path in sorted(case_dir.rglob("*")):
            if not path.is_file():
                continue
            text = read_text(path)
            if not text:
                continue
            if is_decision(text):
                decisions.append(text)
            else:
                materials.append(text)
        if not decisions:
            continue
        record = dict(meta.get(case_dir.name, {}))
        record["case_id"] = case_dir.name
        record["decisions"] = decisions
        record["materials"] = "\n\n".join(materials)
        cases.append(record)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=pathlib.Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--db", type=pathlib.Path, default=None)
    parser.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = args.db or (args.snapshot / "courtdraft.db")
    cases = collect_cases(args.snapshot, db)
    print(f"дел с решениями: {len(cases)}")

    template = load_template()
    section = next(item for item in template["sections"] if item["key"] == "established")
    system_text = SYSTEM_PROMPT.format(review=NEEDS_REVIEW, no_law=NO_LAW_FOUND)

    pairs: list[dict] = []
    skipped_short = 0
    skipped_anon = 0

    for case in cases:
        materials = case["materials"].strip()
        for text in case["decisions"]:
            parts = split_sections(clean_decision(text))
            target = parts.established.strip()
            if not target:
                continue
            source = materials or parts.descriptive
            if len(source) < MIN_MATERIALS:
                skipped_short += 1
                continue
            if len(target) < MIN_TARGET:
                skipped_short += 1
                continue
            if has_anonymisation(source) or has_anonymisation(target):
                skipped_anon += 1
                continue

            record = {
                "case_number": case.get("case_number", ""),
                "court_name": case.get("court_name", ""),
                "category": case.get("category", ""),
                "case_type": case.get("case_type", ""),
                "target": target,
            }
            pack = pack_for(record, source)
            prompt, _citations = build_section_prompt(section, pack)
            pairs.append(
                {
                    "messages": [
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": target},
                    ],
                    "meta": {
                        "source": "snapshot",
                        "case_number": record["case_number"],
                        "case_id": case["case_id"],
                        "section": "established",
                        "split": split_of(case["case_id"]),
                        "prompt_chars": len(prompt),
                        "target_chars": len(target),
                    },
                }
            )

    train = sum(1 for pair in pairs if pair["meta"]["split"] == "train")
    val = len(pairs) - train
    print(f"пар из снимка: {len(pairs)} (train {train}, val {val})")
    print(f"пропущено: коротких {skipped_short}, с обезличиванием {skipped_anon}")

    corpus: list[dict] = []
    if args.corpus.exists():
        with args.corpus.open(encoding="utf-8") as handle:
            corpus = [json.loads(line) for line in handle if line.strip()]
        print(f"корпусных пар: {len(corpus)}")

    merged = corpus + pairs
    print(f"итого пар: {len(merged)}")

    if args.dry_run:
        print("dry-run: файл не записан")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for pair in merged:
            handle.write(json.dumps(pair, ensure_ascii=False) + "\n")
    size = args.output.stat().st_size / 1024 / 1024
    print(f"записано: {args.output} ({size:.1f} МБ)")


if __name__ == "__main__":
    main()
