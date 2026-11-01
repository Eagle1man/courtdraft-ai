"""20–30 обезличенных фрагментов из реальных дел — для проверки фильтра.

Синтетика и переводы не покрывают живые формулировки: в реальных абзацах
встречаются «суд обязал», «прошу обязать», «не согласен с решением» — их
модель может принять за команду. Поэтому в обучающий набор добавляются
настоящие абзацы, уже обезличенные в проекте (`data/demo/real`).

**Приватность:** исходники лежат локально и в git не входят; файл
фрагментов тоже добавлен в `.gitignore`. В репозиторий не попадают ни
исходные документы, ни нарезка.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\make_real_benign_fragments.py `
        --source data\\demo\\real --limit 25

Пишет `data\\real_benign_fragments.jsonl` в формате общего набора:
`{"text", "label": "benign", "source": "real_case_fragment"}`.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "real_benign_fragments.jsonl"
SEED = 20261101

#: Файлы, которые не являются текстом дела.
SKIP_SUFFIXES = {".json", ".log", ".png", ".jpg", ".pdf", ".docx", ".db"}


def clean(fragment: str) -> str:
    return " ".join(fragment.split())


def is_russian(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    cyrillic = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    return cyrillic / len(letters) > 0.7


def collect(source: Path, min_len: int, max_len: int) -> list[tuple[str, str]]:
    """(текст, имя файла) — абзацы нужной длины и на русском."""
    found: list[tuple[str, str]] = []
    for path in sorted(source.rglob("*.txt")):
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for paragraph in re.split(r"\n\s*\n", text):
            fragment = clean(paragraph)
            if not (min_len <= len(fragment) <= max_len):
                continue
            if not is_russian(fragment):
                continue
            # Шапки с реквизитами и адресами — не факты и не формулировки.
            if re.search(r"(ИНН|ОГРН|КПП|БИК|р/с|к/с|индекс|тел\.|факс|e-?mail)", fragment):
                continue
            found.append((fragment, path.name))
    return found


def pick(items: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    rnd = random.Random(SEED)
    rnd.shuffle(items)
    seen: set[str] = set()
    picked: list[tuple[str, str]] = []
    for text, name in items:
        key = re.sub(r"\W+", "", text.lower())[:100]
        if key in seen:
            continue
        seen.add(key)
        picked.append((text, name))
        if len(picked) >= limit:
            break
    return picked


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT / "data" / "demo" / "real"))
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--min-len", type=int, default=150)
    parser.add_argument("--max-len", type=int, default=600)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"нет каталога с реальными делами: {source}")
        return 2

    items = collect(source, args.min_len, args.max_len)
    picked = pick(items, args.limit)
    if not picked:
        print("не нашлось подходящих абзацев")
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(
            json.dumps(
                {
                    "text": text,
                    "label": "benign",
                    "source": f"real_case_fragment:{name}",
                },
                ensure_ascii=False,
            )
            for text, name in picked
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"фрагментов: {len(picked)} из {len({name for _, name in picked})} файлов")
    print(f"записано: {out} (локально, в git не входит)")
    print("проверьте 5–10 фрагментов глазами: не осталось ли персональных данных")
    return 0


if __name__ == "__main__":
    sys.exit(main())
