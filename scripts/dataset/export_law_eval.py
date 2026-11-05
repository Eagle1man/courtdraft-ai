"""Выгрузить проверочный набор для поиска по нормам из триплетов.

В триплетах есть только идентификатор правильной нормы (код, статья,
пункт) и текст негатива — текста положительного примера нет. Поэтому
для обучения retriever их мало, а для проверки индекса достаточно:
запрос → ожидаемая статья.

Набор нужен, чтобы измерять качество поиска норм на стенде, а не на
глаз. Фильтр по кодексу — потому что в базе знаний пока только ГК РФ.

Запуск:
    python scripts\\dataset\\export_law_eval.py
    python scripts\\dataset\\export_law_eval.py --code "ГПК РФ"
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

import pyarrow.parquet as pq

sys.stdout.reconfigure(errors="replace")

SOURCE = r"D:\courtdraft-datasets\retrieval-triplets.parquet"
OUTPUT = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "backend"
    / "data"
    / "eval"
    / "laws-retrieval-gk.jsonl"
)

_ARTICLE = re.compile(r"Статья\s+(\d+(?:\.\d+)?)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    parser.add_argument("--code", default="ГК РФ")
    args = parser.parse_args()

    table = pq.read_table(args.source, columns=["query", "positive_id", "query_type"])
    rows = 0
    skipped = 0
    types: collections.Counter[str] = collections.Counter()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", encoding="utf-8") as handle:
        for item in table.to_pylist():
            identifier = item.get("positive_id") or ""
            if not identifier.startswith(args.code):
                continue
            parts = [piece.strip() for piece in identifier.split("|")]
            match = _ARTICLE.search(parts[1]) if len(parts) > 1 else None
            if not match:
                skipped += 1
                continue
            record = {
                "query": item["query"],
                "code": parts[0],
                "article": match.group(1),
                "part": parts[2] if len(parts) > 2 else "",
                "query_type": item.get("query_type", ""),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            rows += 1
            types[record["query_type"]] += 1

    print(f"кодекс:      {args.code}")
    print(f"запросов:    {rows}")
    print(f"пропущено:   {skipped}")
    print(f"типы:        {dict(types)}")
    print(f"размер:      {args.output.stat().st_size / 1024:.0f} КБ")
    print(f"записано:    {args.output}")


if __name__ == "__main__":
    main()
