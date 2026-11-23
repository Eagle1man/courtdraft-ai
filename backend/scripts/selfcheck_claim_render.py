"""Побайтовая сверка рендера с эталоном. Печатает первое расхождение."""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
FIX = BACKEND / "tests" / "fixtures" / "case_2-101-2025"


def main() -> int:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim
    from app.textnorm import broken_numbers, normalize

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    rendered = render_claim(ClaimModel.from_passport(passport))
    actual = normalize(rendered.text)
    golden = normalize((FIX / "expected_claim.txt").read_text(encoding="utf-8"))

    print(f"знаков: {len(actual)} (эталон {len(golden)})")
    print(f"разрывы чисел: {broken_numbers(actual) or 'нет'}")
    print(f"пробелы данных: {list(rendered.findings)}")

    if actual == golden:
        print("СОВПАДАЕТ ПОБАЙТОВО")
        return 0
    index = next(
        (i for i, (a, b) in enumerate(zip(golden, actual)) if a != b),
        min(len(golden), len(actual)),
    )
    print(f"ПЕРВОЕ РАСХОЖДЕНИЕ на смещении {index}")
    print(f"  эталон: {golden[max(0, index - 60):index + 60]!r}")
    print(f"  рендер: {actual[max(0, index - 60):index + 60]!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())