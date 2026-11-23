"""Замер времени детерминированного рендера иска (критерий 2 Приложения Е)."""
from __future__ import annotations

import json
import time
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "case_2-101-2025"


def main() -> int:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    model = ClaimModel.from_passport(passport)

    render_claim(model)  # прогрев

    runs = []
    for _ in range(10):
        t0 = time.perf_counter()
        render_claim(model)
        runs.append((time.perf_counter() - t0) * 1000.0)

    runs.sort()
    median = runs[len(runs) // 2]
    print(f"прогонов: {len(runs)}")
    print(f"медиана:  {median:8.1f} мс")
    print(f"мин:      {runs[0]:8.1f} мс")
    print(f"макс:     {runs[-1]:8.1f} мс")
    print(f"порог:    1000 мс -> {'ПРОЙДЕН' if median < 1000 else 'ПРЕВЫШЕН'}")
    return 0 if median < 1000 else 2


if __name__ == "__main__":
    raise SystemExit(main())
