"""Адаптер reader к уже извлечённым документам БД.

Новый модуль не читает файлы повторно и не запускает OCR. Источник текста —
`CaseDocument.text` и `DocumentChunk` существующего upload-контура.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

from .models import DocIntake, Fragment, Locator


def _format(filename: str, mime_type: str = "") -> str:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in {".txt", ".md"}:
        return "txt"
    if suffix == ".docx":
        return "docx"
    if suffix == ".pdf":
        return "pdf"
    if "json" in mime_type:
        return "json"
    return "unknown"


def fragments_from_text(document_id: str, filename: str, text: str) -> list[Fragment]:
    """Разбить уже извлечённый текст на блоки с офсетами."""
    source = text or ""
    fragments: list[Fragment] = []
    position = 0
    for index, block in enumerate(re.split(r"\n\s*\n", source)):
        value = block.strip()
        if not value:
            continue
        start = source.find(value, position)
        if start < 0:
            start = position
        end = start + len(value)
        position = end
        fragments.append(
            Fragment(
                locator=Locator(
                    document_id=document_id,
                    filename=filename,
                    block=index,
                    start=start,
                    end=end,
                ),
                text=value,
            )
        )
    return fragments


def from_db_documents(
    documents: Iterable[Any],
) -> tuple[list[DocIntake], dict[str, list[Fragment]]]:
    """Преобразовать `CaseDocument` + `DocumentChunk` в модели модуля."""
    intakes: list[DocIntake] = []
    fragments_by_doc: dict[str, list[Fragment]] = {}
    seen_sha: dict[str, str] = {}

    for document in documents:
        document_id = str(document.id)
        text = str(document.text or "")
        digest = str(document.sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest())
        duplicate_of = seen_sha.get(digest)
        if duplicate_of is None:
            seen_sha[digest] = document_id

        intake = DocIntake(
            document_id=document_id,
            filename=str(document.filename),
            size=len(text.encode("utf-8")),
            sha256=digest,
            format=_format(str(document.filename), str(document.mime_type or "")),
            pages=int(document.page_count or 0) or None,
            duplicate_of=duplicate_of,
            read_ok=bool(text.strip()),
            errors=[] if text.strip() else ["в БД нет извлечённого текста"],
        )
        intakes.append(intake)

        fragments: list[Fragment] = []
        chunks = list(getattr(document, "chunks", []) or [])
        if chunks:
            for chunk in sorted(chunks, key=lambda item: item.chunk_index):
                chunk_text = str(chunk.text or "").strip()
                if not chunk_text:
                    continue
                fragments.append(
                    Fragment(
                        locator=Locator(
                            document_id=document_id,
                            filename=intake.filename,
                            page=chunk.page,
                            block=chunk.paragraph,
                            start=chunk.character_start,
                            end=chunk.character_end,
                        ),
                        text=chunk_text,
                    )
                )
        if not fragments:
            fragments = fragments_from_text(document_id, intake.filename, text)
        fragments_by_doc[document_id] = fragments

    return intakes, fragments_by_doc


def intake_one(document_id: str, filename: str, data: bytes) -> DocIntake:
    """Метаданные файла без повторного извлечения (для совместимости тестов)."""
    return DocIntake(
        document_id=document_id,
        filename=filename,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        format=_format(filename),
    )
