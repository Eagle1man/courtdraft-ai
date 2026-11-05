"""Сравнить два прогона A/B, собранные ab_collect.py.

Печатает по каждому делу: статус, замечания, утверждения без источника,
длину установительной и мотивировочной частей, время. Итоговая строка —
что изменилось в среднем.

Запуск:
    python scripts\\diagnostics\\ab_compare.py ab-base1p5.json ab-tuned7b.json
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.stdout.reconfigure(errors="replace")

TRACKED = ("motivating", "established", "operative")


def _load(path: str) -> dict[str, object]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _index(data: dict[str, object]) -> dict[str, dict]:
    cases = data.get("cases") or []
    return {row["case_id"]: row for row in cases}


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("нужно два файла: ab-base.json ab-tuned.json")

    before, after = _load(sys.argv[1]), _load(sys.argv[2])
    left, right = _index(before), _index(after)

    print(f"{'дело':<22} {'статус':<18} {'замеч.':>10} {'без ист.':>10} {'мотив.':>9} {'сек.':>7}")
    metrics = {"issues": ([], []), "unsupported": ([], []), "seconds": ([], [])}

    for case_id, old in left.items():
        new = right.get(case_id)
        if new is None:
            continue
        old_m = old.get("sections", {}).get("motivating", 0)
        new_m = new.get("sections", {}).get("motivating", 0)
        print(
            f"{old.get('case_number', case_id)[:21]:<22} "
            f"{str(old.get('status'))[:8]:>8} -> {str(new.get('status'))[:8]:<8} "
            f"{old.get('issues_total', 0):>4} -> {new.get('issues_total', 0):<4} "
            f"{old.get('unsupported_claims', 0):>4} -> {new.get('unsupported_claims', 0):<4} "
            f"{old_m:>4} -> {new_m:<4} "
            f"{old.get('seconds', 0):>5} -> {new.get('seconds', 0):<5}"
        )
        metrics["issues"][0].append(float(old.get("issues_total", 0)))
        metrics["issues"][1].append(float(new.get("issues_total", 0)))
        metrics["unsupported"][0].append(float(old.get("unsupported_claims", 0)))
        metrics["unsupported"][1].append(float(new.get("unsupported_claims", 0)))
        metrics["seconds"][0].append(float(old.get("seconds", 0)))
        metrics["seconds"][1].append(float(new.get("seconds", 0)))

    print("\nсредние:")
    for key, (old_values, new_values) in metrics.items():
        print(
            f"  {key:<14} {_avg(old_values):8.1f} -> {_avg(new_values):8.1f}"
        )


if __name__ == "__main__":
    main()
