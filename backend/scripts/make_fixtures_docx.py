"""Собрать DOCX-версии эталонных материалов с НАТИВНЫМИ таблицами.

Зачем: в текущих материалах таблицы набраны pipe-разметкой внутри DOCX,
и extract._render_table превращает их в текст с '|', который затем
захватывается как значение поля. Эталон обязан быть в правильном формате.

Запуск:
    python backend/scripts/make_fixtures_docx.py
"""
from __future__ import annotations

import re
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "case_2-101-2025"
SRC = FIXTURES / "materials"
DST = FIXTURES / "materials_docx"

_PIPE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_SEPARATOR = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def build(md_text: str, target: Path) -> None:
    from docx import Document
    from docx.shared import Cm, Pt

    doc = Document()
    for section in doc.sections:
        section.left_margin = Cm(3.0)
        section.right_margin = Cm(1.5)
        section.top_margin = Cm(2.0)
        section.bottom_margin = Cm(2.0)
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(13)

    lines = md_text.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if _PIPE_ROW.match(line):
            rows: list[list[str]] = []
            while index < len(lines) and _PIPE_ROW.match(lines[index]):
                if not _SEPARATOR.match(lines[index]):
                    rows.append(_cells(lines[index]))
                index += 1
            if not rows:
                continue
            width = max(len(r) for r in rows)
            table = doc.add_table(rows=0, cols=width)
            table.style = "Table Grid"
            for row in rows:
                cells = table.add_row().cells
                for position in range(width):
                    cells[position].text = row[position] if position < len(row) else ""
            continue
        stripped = line.strip()
        if stripped in {"---", ""}:
            if stripped == "---":
                doc.add_paragraph()
            index += 1
            continue
        if stripped.startswith("#"):
            doc.add_paragraph(stripped.lstrip("# ").strip())
        else:
            doc.add_paragraph(stripped)
        index += 1

    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(target))


def main() -> int:
    if not SRC.is_dir():
        print(f"НЕТ КАТАЛОГА: {SRC}")
        return 2
    DST.mkdir(parents=True, exist_ok=True)
    built = 0
    for path in sorted(SRC.glob("*.md")):
        target = DST / (path.stem + ".docx")
        build(path.read_text(encoding="utf-8"), target)
        built += 1
        print(f"OK {target.name}")
    print(f"Собрано DOCX: {built}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
