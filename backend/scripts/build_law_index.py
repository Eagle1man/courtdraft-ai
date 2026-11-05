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
from app.datasets.law_corpus import build_chunks
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=pathlib.Path, default=DEFAULT_PDF)
    parser.add_argument("--code", default="ГК РФ")
    parser.add_argument("--download", action="store_true", help="скачать PDF, если нет")
    parser.add_argument("--dry-run", action="store_true", help="только разбор, без индексации")
    parser.add_argument(
        "--dump",
        type=pathlib.Path,
        default=None,
        help="сохранить пассажи в JSON (удобно переносить на стенд)",
    )
    args = parser.parse_args()

    if not args.pdf.exists():
        if args.download:
            _download(args.pdf)
        else:
            raise SystemExit(
                f"Нет файла {args.pdf}. Запустите с --download или укажите --pdf."
            )

    text = _extract_text(args.pdf)
    chunks = build_chunks(text, code=args.code)
    lengths = [len(chunk.text) for chunk in chunks]

    print(f"кодекс:            {args.code}")
    print(f"знаков в тексте:   {len(text)}")
    print(f"пассажей:          {len(chunks)}")
    print(f"статей:            {len({chunk.article for chunk in chunks})}")
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
