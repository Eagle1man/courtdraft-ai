"""Извлечь гражданские решения из parquet-выгрузки sudrf в JSONL.

Что делает: берёт `full_text` страницы, вырезает тело решения после
«Именем Российской Федерации», чистит разметку, определяет вид дела по
номеру, отбрасывает пустое и короткое, убирает дубли и пишет JSONL.

Зачем: этот корпус — реальные решения судов общей юрисдикции. Он нужен
для двух вещей:
  * RAG по похожим делам;
  * обучение на парах «установленные факты → мотивы и резолюция».

В git корпус не попадает: это выгрузка на гигабайты.

Запуск:
    python scripts\\dataset\\extract_decisions.py --dry-run --limit 5000
    python scripts\\dataset\\extract_decisions.py \\
        --output D:\\courtdraft-datasets\\court-decisions-feb2026.jsonl
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import sys

import pyarrow.parquet as pq

from app.datasets.court_decisions import (
    case_kind,
    clean_decision,
    decision_date,
    extract_decision_body,
    split_sections,
)

sys.stdout.reconfigure(errors="replace")

DEFAULT_INPUT = r"D:\загрузки Chrome\judicial_real_feb2026.parquet"
DEFAULT_OUTPUT = r"D:\courtdraft-datasets\court-decisions-feb2026.jsonl"

#: Минимальная длина решения. Ниже — обрывок страницы, а не акт.
MIN_CHARS = 1500


def _fingerprint(text: str) -> str:
    """Отпечаток текста для дедупликации.

    Нормализуется пробел и регистр: одно и то же решение встречается на
    странице дела и в списке заседаний, отличаясь разметкой.
    """
    normalized = re.sub(r"\s+", " ", text.lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _iter_rows(path: str):
    """Идти по строкам по группам, не читая файл целиком в память."""
    parquet = pq.ParquetFile(path)
    for group in range(parquet.metadata.num_row_groups):
        table = parquet.read_row_group(
            group, columns=["case_number", "court_id", "planned_date", "full_text"]
        )
        yield from table.to_pylist()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=0, help="0 — весь файл")
    parser.add_argument("--dry-run", action="store_true", help="только статистика")
    parser.add_argument(
        "--all-kinds",
        action="store_true",
        help="не фильтровать по виду дела (по умолчанию только гражданские)",
    )
    args = parser.parse_args()

    kinds: collections.Counter[str] = collections.Counter()
    total = 0
    with_decision = 0
    kept = 0
    duplicates = 0
    short = 0
    no_operative = 0
    lengths: list[int] = []
    seen: set[str] = set()

    output = pathlib.Path(args.output)
    handle = None
    if not args.dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = output.open("w", encoding="utf-8")

    try:
        for row in _iter_rows(args.input):
            total += 1
            body = extract_decision_body(row.get("full_text") or "")
            if not body:
                continue
            with_decision += 1

            text = clean_decision(body)
            kind = case_kind(row.get("case_number") or "", text)
            kinds[kind] += 1

            if not args.all_kinds and kind != "civil":
                continue
            if len(text) < MIN_CHARS:
                short += 1
                continue

            parts = split_sections(text)
            if len(parts.operative) < 50:
                no_operative += 1
                continue

            fingerprint = _fingerprint(text)
            if fingerprint in seen:
                duplicates += 1
                continue
            seen.add(fingerprint)

            kept += 1
            lengths.append(len(text))

            if handle is not None:
                record = {
                    "case_number": row.get("case_number") or "",
                    "court_id": row.get("court_id") or "",
                    "planned_date": str(row.get("planned_date") or ""),
                    "decision_date": decision_date(text),
                    "kind": kind,
                    "chars": len(text),
                    "text": text,
                    "descriptive": parts.descriptive,
                    "established": parts.established,
                    "operative": parts.operative,
                    "fingerprint": fingerprint,
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

            if args.limit and kept >= args.limit:
                break
    finally:
        if handle is not None:
            handle.close()

    lengths.sort()
    median = lengths[len(lengths) // 2] if lengths else 0
    print(f"строк прочитано:            {total}")
    print(f"с текстом решения:          {with_decision}")
    print(f"виды дел:                   {dict(kinds.most_common())}")
    print(f"отобрано:                   {kept}")
    print(f"медиана длины решения:      {median}")
    print(f"отброшено коротких:         {short}")
    print(f"без резолютивной части:     {no_operative}")
    print(f"дублей:                     {duplicates}")
    if args.dry_run:
        print("dry-run: файл не записан")
    else:
        print(f"записано: {output} ({output.stat().st_size / 1024 / 1024:.0f} МБ)")


if __name__ == "__main__":
    main()
