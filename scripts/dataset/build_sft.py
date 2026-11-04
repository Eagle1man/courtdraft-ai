"""Собрать SFT-датасет из корпуса решений в формате наших промптов.

Что делает: для каждого решения строит пару «описательная часть →
установительная часть» и упаковывает её в тот же формат, который
сервис использует при генерации (`build_section_prompt` + системный
промпт помощника судьи). Это принципиально: если учить на другом
формате, модель выучит не тот язык, который нужен на инференсе.

Почему именно установительная часть: она собирается моделью (в отличие
от резолютивной), она же была самой слабой на живом тесте — пустой или
пересказом фактов.

Фильтры:
  * нет маркеров обезличивания `<…>` — иначе модель научится писать
    «<ФИО>» вместо имени из материалов;
  * описательная от 200 знаков, установительная от 300;
  * дедупликация по отпечатку решения.

Разбиение train/val — по делу, а не по паре: иначе половинки одного
решения попадут в обе части.

Запуск:
    python scripts\\dataset\\build_sft.py --limit 200 --dry-run
    python scripts\\dataset\\build_sft.py \\
        --output D:\\courtdraft-datasets\\sft-established-feb2026.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

from app.agents.draft_agent import (
    NEEDS_REVIEW,
    NO_LAW_FOUND,
    SYSTEM_PROMPT,
    build_section_prompt,
)
from app.agents.evidence_pack import EvidenceItem, EvidencePack, load_template
from app.drafting.date_guard import extract_dates
from app.drafting.numeric_guard import extract_money

sys.stdout.reconfigure(errors="replace")

DEFAULT_INPUT = r"D:\courtdraft-datasets\court-decisions-feb2026.jsonl"
DEFAULT_OUTPUT = r"D:\courtdraft-datasets\sft-established-feb2026.jsonl"

MIN_DESCRIPTIVE = 200
MIN_ESTABLISHED = 300


def _has_anonymization(text: str) -> bool:
    """Есть ли в тексте маркер обезличивания вида `<ФИО>`."""
    return "<" in text or ">" in text


def _split_of(case_number: str, court_id: str) -> str:
    """Стабильное отнесение дела к train/val.

    Считается по делу, поэтому все пары одного решения попадают в одну
    часть. Порог 20 — примерно 5 % на проверку.
    """
    key = f"{court_id}|{case_number}".encode()
    digest = hashlib.sha256(key).hexdigest()
    return "val" if int(digest[:8], 16) % 20 == 0 else "train"


def _pack_for(record: dict, template_section: dict) -> EvidencePack:
    """Пакет доказательств из самого решения.

    Внешнее решение — не материалы дела студента, поэтому роли
    приблизительные: описательная часть идёт как требования и доводы,
    а полный текст — как документ. Иного источника у нас нет, и
    притворяться, что есть, нельзя.
    """
    descriptive = record["descriptive"]
    established = record["established"]
    joined = f"{descriptive}\n{established}"

    pack = EvidencePack(
        case_number=record.get("case_number", ""),
        court_name="",
        category="",
        jurisdiction="civil",
    )
    pack.documents["решение.txt"] = descriptive
    pack.items.append(
        EvidenceItem(
            category="claims",
            text=descriptive[:6000],
            source_document="решение.txt",
            source_type="COURT_DECISION",
            order=1,
        )
    )
    pack.amounts = [raw for raw, _ in extract_money(joined)]
    pack.dates = [raw for raw, _ in extract_dates(joined)]
    return pack


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=0, help="0 — весь корпус")
    parser.add_argument(
        "--max-target",
        type=int,
        default=8000,
        help=(
            "максимальная длина ответа в знаках; длиннее — пропуск, а не "
            "обрезка: обрезанный ответ учит недоговаривать мотивы"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="только статистика")
    args = parser.parse_args()

    template = load_template()
    section = next(
        item for item in template["sections"] if item["key"] == "established"
    )
    system_text = SYSTEM_PROMPT.format(review=NEEDS_REVIEW, no_law=NO_LAW_FOUND)

    output = pathlib.Path(args.output)
    handle = None
    if not args.dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = output.open("w", encoding="utf-8")

    read = 0
    anonymized = 0
    short = 0
    long_target = 0
    written = 0
    train = 0
    val = 0
    prompt_lengths: list[int] = []
    target_lengths: list[int] = []

    try:
        with open(args.input, encoding="utf-8") as source:
            for line in source:
                read += 1
                record = json.loads(line)
                descriptive = record["descriptive"]
                established = record["established"]

                if _has_anonymization(descriptive) or _has_anonymization(established):
                    anonymized += 1
                    continue
                if (
                    len(descriptive) < MIN_DESCRIPTIVE
                    or len(established) < MIN_ESTABLISHED
                ):
                    short += 1
                    continue
                if len(established) > args.max_target:
                    long_target += 1
                    continue

                pack = _pack_for(record, section)
                prompt, _citations = build_section_prompt(section, pack)
                split = _split_of(
                    record.get("case_number", ""), record.get("court_id", "")
                )

                example = {
                    "messages": [
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": established},
                    ],
                    "meta": {
                        "case_number": record.get("case_number", ""),
                        "court_id": record.get("court_id", ""),
                        "decision_date": record.get("decision_date", ""),
                        "section": "established",
                        "split": split,
                        "prompt_chars": len(prompt),
                        "target_chars": len(established),
                    },
                }
                if handle is not None:
                    handle.write(json.dumps(example, ensure_ascii=False) + "\n")

                written += 1
                train += split == "train"
                val += split == "val"
                prompt_lengths.append(len(prompt))
                target_lengths.append(len(established))

                if args.limit and written >= args.limit:
                    break
    finally:
        if handle is not None:
            handle.close()

    prompt_lengths.sort()
    target_lengths.sort()

    def _median(values: list[int]) -> int:
        return values[len(values) // 2] if values else 0

    print(f"прочитано решений:          {read}")
    print(f"собрано примеров:           {written}")
    print(f"  train / val:              {train} / {val}")
    print(f"пропущено: обезличено       {anonymized}")
    print(f"пропущено: коротко          {short}")
    print(f"пропущено: ответ длиннее    {long_target}")
    print(f"медиана промпта:            {_median(prompt_lengths)}")
    print(f"медиана ответа:             {_median(target_lengths)}")
    if args.dry_run:
        print("dry-run: файл не записан")
    else:
        size = output.stat().st_size / 1024 / 1024
        print(f"записано: {output} ({size:.0f} МБ)")


if __name__ == "__main__":
    main()
