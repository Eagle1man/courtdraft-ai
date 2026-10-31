"""Базовый benchmark проверок (Этап 1).

Что измеряется:
  1. Резолютивная часть — на размеченном датасете tests/data/resol_fixtures.json
     (каждая проверка имеет defect-фикстуру, есть clean и disputed).
  2. Суммы — на синтетических примерах numeric_guard.
  3. Подсудность — на синтетических примерах jurisdiction.

Это измеряет ПРАВИЛА, а не качество на реальных делах. Датасет
синтетический, поэтому цифры означают «правило срабатывает там, где
должно», а не «система ловит ошибки судей».

Запуск: backend\\.venv\\Scripts\\python.exe scripts\\verification_benchmark.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.drafting.numeric_guard import check_unsourced_amounts
from app.verification.resol_checks import check_resolution
from app.verification.jurisdiction import check_jurisdiction


def load_corpus():
    spec = importlib.util.spec_from_file_location(
        "resol_corpus", ROOT / "backend" / "tests" / "resol_corpus.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["resol_corpus"] = module
    spec.loader.exec_module(module)
    return module.load()


def benchmark_resolution():
    checks, fixtures = load_corpus()
    text_fixtures = [f for f in fixtures if f.kind in {"defect", "clean"}]
    expected_total = 0
    found_total = 0
    false_positive = 0
    clean_count = 0
    for fixture in text_fixtures:
        fired = {d.id for d in check_resolution(fixture.text, fixture.meta)}
        if fixture.kind == "clean":
            clean_count += 1
            false_positive += len(fired)
            continue
        expected_total += len(fixture.expected)
        found_total += len(fixture.expected & fired)
    return {
        "checks": len(checks["checks"]),
        "fixtures": len(text_fixtures),
        "clean": clean_count,
        "expected": expected_total,
        "found": found_total,
        "false_positive": false_positive,
    }


def benchmark_amounts():
    docs = ["Долг 80 000 руб., проценты 9 122,19 руб., пошлина 4 000 руб."]
    cases = [
        ("выдуманная сумма 6 470", "Взыскать 6 470 руб. расходов.", True),
        ("подтверждённая сумма", "Взыскать 80 000 руб. долга.", False),
        ("итог из слагаемых", "Всего 93 122,19 руб.", False),
        ("формат 9122,19 / 9 122,19", "Проценты 9 122,19 руб.", False),
    ]
    correct = 0
    for _, text, should_flag in cases:
        flagged = bool(check_unsourced_amounts(text, docs))
        if flagged == should_flag:
            correct += 1
    return {"cases": len(cases), "correct": correct}


def benchmark_jurisdiction():
    cases = [
        # (цена, тексты, должен ли быть дефект)
        (89_122, ["Мировой судья судебного участка № 123"], True),
        (40_000, ["Мировой судья судебного участка № 123"], False),
        (150_000, ["Районный суд"], False),
        (40_000, ["Районный суд"], True),
        (89_122, ["Мировой судья", "защита прав потребителей"], False),
    ]
    correct = 0
    for price, texts, should_flag in cases:
        flagged = bool(check_jurisdiction(texts, price))
        if flagged == should_flag:
            correct += 1
    return {"cases": len(cases), "correct": correct}


def main() -> None:
    res = benchmark_resolution()
    amt = benchmark_amounts()
    jur = benchmark_jurisdiction()

    recall = res["found"] / res["expected"] if res["expected"] else 0
    print("=== РЕЗОЛЮТИВНАЯ ЧАСТЬ (датасет фикстур) ===")
    print(f"проверок в реестре: {res['checks']}")
    print(f"фикстур defect+clean: {res['fixtures']} (clean: {res['clean']})")
    print(f"ожидаемых срабатываний: {res['expected']}, найдено: {res['found']} ({recall:.0%})")
    print(f"ложных срабатываний на clean: {res['false_positive']}")

    print("\n=== СУММЫ (синтетика) ===")
    print(f"верно: {amt['correct']}/{amt['cases']}")

    print("\n=== ПОДСУДНОСТЬ (синтетика) ===")
    print(f"верно: {jur['correct']}/{jur['cases']}")

    summary = {
        "resolution": res,
        "amounts": amt,
        "jurisdiction": jur,
    }
    out = ROOT / "docs" / "verification-benchmark-baseline.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nсохранено: {out}")


if __name__ == "__main__":
    main()
