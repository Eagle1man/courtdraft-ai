#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подготовка датасета для дообучения Vikhr 7B.

Читает .txt и .docx из D:\courtdraft-ai\data\laws\raw (рекурсивно),
формирует dataset.jsonl в формате:
{"query": "<имя файла без расширения>: <первые 100 символов текста>", "response": "<текст ≤1500 символов по границе слова>"}

Ограничение: один пример на файл (~4000 файлов). response ограничен 1500 символами.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple


RAW_DIR = Path(r"D:\courtdraft-ai\data\laws\raw")
OUT_PATH = Path(r"D:\courtdraft-ai\dataset.jsonl")

ENCODINGS = ("utf-8", "utf-8-sig", "cp1251", "koi8-r", "latin-1")
INVISIBLE = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad", "\u2060")
WS_RE = re.compile(r"[ \t]+")
MULTI_NL_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    for inv in INVISIBLE:
        t = t.replace(inv, "")
    t = WS_RE.sub(" ", t)
    t = MULTI_NL_RE.sub("\n\n", t)
    return t.strip()


def decode_bytes(data: bytes) -> str:
    for enc in ENCODINGS:
        try:
            s = data.decode(enc)
        except Exception:
            continue
        if enc in ("utf-8", "utf-8-sig"):
            return s
        # rough russian check
        sample = s[:2000]
        cyr = sum(1 for ch in sample if 'а' <= ch.lower() <= 'я' or ch == 'ё')
        mojibake = sum(1 for ch in sample if ch in "¤­°±²µ¶·¸¹º")
        if cyr >= mojibake:
            return s
    return data.decode("utf-8", errors="replace")


def extract_txt(path: Path) -> str:
    try:
        data = path.read_bytes()
        return decode_bytes(data)
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать TXT: {e}") from e


def extract_docx(path: Path) -> str:
    try:
        import io
        import docx  # type: ignore
    except Exception as e:
        raise RuntimeError(f"python-docx не установлен: {e}") from e
    try:
        doc = docx.Document(str(path))
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать DOCX: {e}") from e

    parts: List[str] = []
    for p in doc.paragraphs:
        content = (p.text or "").strip()
        if not content:
            parts.append("")
            continue
        style = (p.style.name or "").lower() if p.style else ""
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            parts.append(f"{'#' * min(int(level), 6)} {content}")
        elif style == "title":
            parts.append(f"# {content}")
        else:
            parts.append(content)
    # tables
    for table in getattr(doc, "tables", []):
        for row in table.rows:
            cells = []
            for cell in row.cells:
                c = " ".join((p.text or "").strip() for p in cell.paragraphs).strip()
                if c:
                    cells.append(c)
            if cells:
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def truncate_to_chars(text: str, max_chars: int = 1500) -> str:
    if len(text) <= max_chars:
        return text
    # cut by word boundary
    cut = text[:max_chars]
    # try to break at whitespace
    m = re.search(r"\s+\S*$", cut)
    if m:
        cut2 = cut[: m.start()]
    else:
        cut2 = cut
    if not cut2:
        cut2 = cut
    return cut2


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Prepare dataset for Vikhr 7B finetune")
    parser.add_argument("--src", type=Path, default=RAW_DIR)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    parser.add_argument("--limit", type=int, default=0, help="Ограничить число файлов для пробного прогона")
    args = parser.parse_args(argv)

    src = args.src
    out = args.out

    if not src.exists():
        print(f"[ERROR] Папка не найдена: {src}")
        print("Создай папку и положи туда .txt и .docx файлы (или укажи --src)")
        return 0

    files: List[Path] = []
    for p in src.rglob("*.txt"):
        files.append(p)
    for p in src.rglob("*.docx"):
        files.append(p)
    # skip temp files
    files = [f for f in files if not f.name.startswith("~$")]

    files.sort()
    if args.limit and args.limit > 0:
        files = files[: args.limit]
        print(f"[INFO] Пробный прогон: берём {len(files)} файлов (--limit {args.limit})")

    if not files:
        print(f"[INFO] В {src} не найдено .txt/.docx файлов. Файл {out} не создан.")
        return 0

    written = 0
    truncated = 0
    skipped = 0
    skipped_reasons = []

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for path in files:
            try:
                ext = path.suffix.lower()
                if ext == ".txt":
                    raw = extract_txt(path)
                elif ext == ".docx":
                    raw = extract_docx(path)
                else:
                    skipped += 1
                    skipped_reasons.append(f"{path.name}: неподдерживаемое расширение")
                    continue

                text = normalize_text(raw)
                if not text:
                    skipped += 1
                    skipped_reasons.append(f"{path.name}: пустой текст")
                    continue
                if len(text) < 100:
                    skipped += 1
                    skipped_reasons.append(f"{path.name}: <100 символов ({len(text)})")
                    continue

                title = path.stem
                first100 = text[:100].replace("\n", " ").strip()
                query = f"{title}: {first100}"

                response_full = text
                if len(response_full) > 1500:
                    response = truncate_to_chars(response_full, 1500)
                    truncated += 1
                else:
                    response = response_full

                record = {"query": query, "response": response}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
            except Exception as e:
                skipped += 1
                skipped_reasons.append(f"{path.name}: {e}")
                continue

    print("=" * 60)
    print(f"Источник: {src}")
    print(f"Выход:    {out}")
    print(f"Файлов найдено/обработано: {len(files)}")
    print(f"Записей в dataset.jsonl:  {written}")
    print(f"Обрезано до 1500 символов: {truncated}")
    print(f"Пропущено:                {skipped}")
    if skipped_reasons:
        print("\nПричины пропуска (первые 20):")
        for r in skipped_reasons[:20]:
            print(f"  - {r}")
        if len(skipped_reasons) > 20:
            print(f"  ... и ещё {len(skipped_reasons)-20}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
