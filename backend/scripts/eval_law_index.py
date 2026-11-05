"""Измерить качество поиска норм по проверочному набору.

Считает recall@1/3/5 и MRR: попал ли номер ожидаемой статьи в первые
k результатов индекса `legal_laws`. Это проверка самой базы знаний, а
не генерации: если поиск не находит норму, модель её и не увидит.

Набор: `backend/data/eval/laws-retrieval-gk.jsonl`, собран из триплетов
(`scripts/dataset/export_law_eval.py`).

Запуск на стенде (где есть bge-m3 и построенный индекс):
    .venv/bin/python -m scripts.eval_law_index
    .venv/bin/python -m scripts.eval_law_index --limit 500 --top-k 5
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from app.config import get_settings
from app.embeddings import build_embedding_provider
from app.rag.laws import search_laws
from app.rag.store import VectorStore

sys.stdout.reconfigure(errors="replace")

DEFAULT_EVAL = (
    pathlib.Path(__file__).resolve().parent.parent
    / "data"
    / "eval"
    / "laws-retrieval-gk.jsonl"
)

_ARTICLE = re.compile(r"Статья\s+(\d+(?:\.\d+)?)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval", type=pathlib.Path, default=DEFAULT_EVAL)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--failures", type=pathlib.Path, default=None)
    args = parser.parse_args()

    if not args.eval.exists():
        raise SystemExit(f"Нет набора {args.eval}: соберите export_law_eval.py")

    rows = []
    with args.eval.open(encoding="utf-8") as handle:
        for line in handle:
            rows.append(json.loads(line))
    if args.limit:
        rows = rows[: args.limit]

    cfg = get_settings()
    store = VectorStore.from_settings(cfg)
    embedder = build_embedding_provider(cfg)

    hits_at_1 = 0
    hits_at_3 = 0
    hits_at_k = 0
    reciprocal = 0.0
    failures = []

    try:
        for row in rows:
            results = await search_laws(
                store, embedder, row["query"], top_k=args.top_k
            )
            found = []
            for result in results:
                match = _ARTICLE.search(result.embedding.filename or "")
                article = match.group(1) if match else ""
                # Совпадение номера статьи из другого кодекса — не попадание:
                # «Статья 1» есть и в АПК, и в ГПК. Без этой проверки recall
                # завышался бы тем сильнее, чем больше кодексов в индексе.
                if result.embedding.document_id != row["code"]:
                    article = ""
                found.append(article)
            expected = row["article"]
            if found and found[0] == expected:
                hits_at_1 += 1
            if expected in found[:3]:
                hits_at_3 += 1
            if expected in found:
                hits_at_k += 1
                reciprocal += 1.0 / (found.index(expected) + 1)
            elif len(failures) < 20:
                failures.append(
                    {
                        "query": row["query"][:300],
                        "expected": expected,
                        "found": found,
                    }
                )
    finally:
        store.client.close()

    total = len(rows)
    print(f"запросов:      {total}")
    print(f"recall@1:      {hits_at_1 / total:.3f}")
    print(f"recall@3:      {hits_at_3 / total:.3f}")
    print(f"recall@{args.top_k}:      {hits_at_k / total:.3f}")
    print(f"MRR:           {reciprocal / total:.3f}")

    if failures and args.failures:
        args.failures.write_text(
            json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"промахи:       {args.failures}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
