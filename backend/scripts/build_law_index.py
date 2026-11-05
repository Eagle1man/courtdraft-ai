"""Построить базу знаний по нормам из текста кодекса.

Что делает: берёт PDF (или скачивает), разбирает на статьи, режет на
пассажи, считает эмбеддинги и кладёт в коллекцию `legal_laws`.

Хранилище берётся из настроек. Для постоянного индекса на стенде:

    QDRANT_MODE=local
    QDRANT_PATH=data/qdrant
    EMBEDDING_PROVIDER=local
    EMBEDDING_MODEL=bge-m3

Запуск:
    python -m scripts.build_law_index --dry-run
    python -m scripts.build_law_index --download
    python -m scripts.build_law_index --pdf data/laws/sources/gk_rf.pdf
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
import urllib.request

from app.config import get_settings
from app.datasets.law_corpus import (
    LawChunk,
    articles_from_records,
    articles_to_records,
    build_chunks_from_articles,
    parse_articles,
)
from app.embeddings import build_embedding_provider
from app.rag.laws import index_law_chunks
from app.rag.store import VectorStore

sys.stdout.reconfigure(errors="replace")

#: Текст ГК РФ. Официальные тексты законов не являются объектом
#: авторского права (ст. 1259 ГК РФ), но конкретный PDF — чужое
#: издание, поэтому в репозиторий он не кладётся: скачивается по
#: требованию и лежит вне git.
SOURCE_URL = (
    "https://raw.githubusercontent.com/BorisTerminator/rag-mcp-agent/"
    "main/garant_grajdansky_kodeks_rf.pdf"
)
DEFAULT_PDF = (
    pathlib.Path(__file__).resolve().parent.parent
    / "data"
    / "laws"
    / "sources"
    / "gk_rf.pdf"
)


def _download(target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"скачивание: {SOURCE_URL}")
    urllib.request.urlretrieve(SOURCE_URL, target)
    print(f"сохранено: {target} ({target.stat().st_size / 1024 / 1024:.1f} МБ)")


def _extract_text(path: pathlib.Path) -> str:
    import pymupdf

    document = pymupdf.open(path)
    return "\n".join(page.get_text() for page in document)


def _read_jsonl(path: pathlib.Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _first_code(path: pathlib.Path) -> str:
    records = _read_jsonl(path)
    return str(records[0].get("code", "")) if records else ""


def _from_jsonl(paths: list[pathlib.Path], *, code: str) -> tuple[list[LawChunk], int]:
    """Собрать пассажи из выгрузок статей, а не из PDF.

    Зачем: текст процессуальных кодексов берётся из HTML, где статья уже
    выделена разметкой. Заставлять такой текст проходить через PDF-разбор
    — значит искать заголовки там, где они и так известны.

    Файлов может быть несколько (ГК, ГПК, АПК): пассажи каждого кодекса
    помечаются своим названием, иначе в подсказке модели ссылка «Статья 1»
    потеряет кодекс.
    """
    by_code: dict[str, list[dict[str, str]]] = {}
    for path in paths:
        for record in _read_jsonl(path):
            record_code = str(record.get("code", "")) or code
            if code and record_code != code:
                continue
            by_code.setdefault(record_code, []).append(record)

    chunks: list[LawChunk] = []
    total = 0
    for record_code, records in by_code.items():
        articles = articles_from_records(records)
        chunks.extend(build_chunks_from_articles(articles, code=record_code))
        total += sum(len(record.get("text", "")) for record in records)
    return chunks, total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=pathlib.Path, default=DEFAULT_PDF)
    parser.add_argument(
        "--jsonl",
        type=pathlib.Path,
        nargs="+",
        default=None,
        help="готовые статьи (JSONL от fetch_law_code) вместо PDF",
    )
    parser.add_argument("--code", default="")
    parser.add_argument("--download", action="store_true", help="скачать PDF, если нет")
    parser.add_argument("--dry-run", action="store_true", help="только разбор, без индексации")
    parser.add_argument(
        "--dump",
        type=pathlib.Path,
        default=None,
        help="сохранить пассажи в JSON (удобно переносить на стенд)",
    )
    parser.add_argument(
        "--dump-articles",
        type=pathlib.Path,
        default=None,
        help="сохранить разобранные статьи в JSONL (для сборки индекса на стенде)",
    )
    args = parser.parse_args()

    if args.jsonl is not None:
        chunks, raw_chars = _from_jsonl(args.jsonl, code=args.code)
        args.code = args.code or _first_code(args.jsonl[0])
    else:
        if not args.pdf.exists():
            if args.download:
                _download(args.pdf)
            else:
                raise SystemExit(
                    f"Нет файла {args.pdf}. Запустите с --download или укажите --pdf."
                )
        text = _extract_text(args.pdf)
        args.code = args.code or "ГК РФ"
        articles = parse_articles(text, code=args.code)
        chunks = build_chunks_from_articles(articles, code=args.code)
        raw_chars = len(text)

        if args.dump_articles is not None:
            args.dump_articles.parent.mkdir(parents=True, exist_ok=True)
            with args.dump_articles.open("w", encoding="utf-8") as handle:
                for record in articles_to_records(articles, code=args.code):
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"статьи сохранены:  {args.dump_articles}")

    lengths = [len(chunk.text) for chunk in chunks]

    codes = sorted({chunk.code for chunk in chunks}) or [args.code]
    print(f"кодексов:          {len(codes)} ({', '.join(codes)})")
    print(f"знаков в тексте:   {raw_chars}")
    print(f"пассажей:          {len(chunks)}")
    print(f"статей:            {len({(chunk.code, chunk.article) for chunk in chunks})}")
    if lengths:
        print(f"медиана пассажа:   {statistics.median(lengths):.0f}")
    parts = sorted({chunk.part for chunk in chunks if chunk.part})
    print(f"части кодекса:     {', '.join(parts) or 'не распознаны'}")

    if args.dump is not None:
        args.dump.parent.mkdir(parents=True, exist_ok=True)
        with args.dump.open("w", encoding="utf-8") as handle:
            for chunk in chunks:
                handle.write(json.dumps(chunk.__dict__, ensure_ascii=False) + "\n")
        print(f"пассажи сохранены: {args.dump}")

    if args.dry_run:
        print("dry-run: индекс не строился")
        return

    cfg = get_settings()
    store = VectorStore.from_settings(cfg)
    embedder = build_embedding_provider(cfg)
    written = asyncio.run(index_law_chunks(store, embedder, chunks))
    print(f"записано в индекс: {written} (режим {cfg.qdrant_mode.value})")
    if written < len(chunks):
        print("ВНИМАНИЕ: записано меньше, чем пассажей — эмбеддинги недоступны?")


if __name__ == "__main__":
    main()
