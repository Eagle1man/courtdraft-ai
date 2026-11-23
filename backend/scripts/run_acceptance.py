"""Единый прогон приёмки: от фикстур до финального чек-листа.

Запуск:  python backend/scripts/run_acceptance.py
Пишет:   docs/acceptance-progress.md
Код возврата: 0 — всё зелёное, 2 — есть провалы.

Скрипт НЕ падает на отсутствующих модулях: он помечает их «не реализовано»,
чтобы прогресс «красное → зелёное» был виден с первого коммита.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # D:\courtdraft-ai
BACKEND = ROOT / "backend"
FIX = BACKEND / "tests" / "fixtures" / "case_2-101-2025"
DOCS = ROOT / "docs"
PY = sys.executable

REQUIRED_FILES = [
    ROOT / "ЗАДАНИЕ-OPENCODE-КАЧЕСТВО.md",
    ROOT / "ФИКСТУРЫ-ПРИЁМКИ-2-101-2025.md",
    ROOT / "РЕФЕРЕНС-CLAIM-MODEL-RENDER.md",
    ROOT / "ФАЙЛ-8-ЕДИНЫЙ-ПРОГОН.md",
]

RESULTS: list[tuple[str, str, str]] = []


def record(step: str, status: str, detail: str = "") -> None:
    RESULTS.append((step, status, detail))
    mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⬜"}.get(status, "•")
    print(f"{mark} {step:<38} {status:<5} {detail}")


def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Выполнить команду, вернуть (код, хвост вывода)."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd or ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-6:]
        return proc.returncode, " | ".join(tail)
    except FileNotFoundError:
        return 127, f"не найден интерпретатор/команда: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "таймаут 600 с"


# ── Шаг 0. Наличие файлов и эталона ──────────────────────────────────────
def step_prereq() -> bool:
    missing = [str(p.name) for p in REQUIRED_FILES if not p.exists()]
    if missing:
        record("Наличие файлов Ф1–Ф8", "FAIL", "нет: " + ", ".join(missing))
        return False
    record("Наличие файлов Ф1–Ф8", "PASS", "")
    if not (FIX / "00-ПАСПОРТ-ДЕЛА.json").exists():
        record("Фикстуры дела 2-101/2025", "FAIL", "нет 00-ПАСПОРТ-ДЕЛА.json")
        return False
    record("Фикстуры дела 2-101/2025", "PASS", "")
    return True


# ── Шаг 1. DOCX-версии материалов ────────────────────────────────────────
def step_docx() -> None:
    script = BACKEND / "scripts" / "make_fixtures_docx.py"
    if not script.exists():
        record("Сборка DOCX-фикстур", "SKIP", "make_fixtures_docx.py ещё нет")
        return
    code, tail = run([PY, str(script)])
    record("Сборка DOCX-фикстур", "PASS" if code == 0 else "FAIL", tail)


# ── Шаг 2. Модули ядра (импорт-проверка) ─────────────────────────────────
def step_modules() -> None:
    sys.path.insert(0, str(BACKEND))
    probes = [
        ("app.money", "parse_money"),
        ("app.money_words", "rubles_to_words"),
        ("app.textnorm", "broken_numbers"),
        ("app.ingestion.slots", "validate_address"),
        ("app.drafting.claim_model", "ClaimModel"),
        ("app.drafting.claim_render", "render_claim"),
        ("app.services.export_gate", "assert_export_allowed"),
        ("app.ingestion.passport", "CasePassport"),
        ("app.fees", "claim_fee_kopecks"),
        ("app.verification.claim_model_checks", "verify_claim_model"),
        ("app.verification.deviations", "build_deviations"),
    ]
    for module, attr in probes:
        try:
            mod = __import__(module, fromlist=[attr])
            if not hasattr(mod, attr):
                raise AttributeError(attr)
            record(f"Модуль {module}", "PASS", "")
        except ModuleNotFoundError:
            record(f"Модуль {module}", "SKIP", "не реализован")
        except Exception as exc:  # noqa: BLE001
            record(f"Модуль {module}", "FAIL", f"{type(exc).__name__}: {exc}")


# ── Шаг 3. Юнит-тесты ────────────────────────────────────────────────────
def step_unit_tests() -> None:
    test_files = [
        "test_money_canon.py", "test_totals_kopecks.py", "test_typed_slots.py",
        "test_claim_golden.py", "test_export_gate.py", "test_idempotency.py",
        "test_draft_separation.py", "test_passport.py",
        "test_claim_model_checks.py", "test_deviations.py", "test_interest.py",
    ]
    existing = [t for t in test_files if (BACKEND / "tests" / t).exists()]
    if not existing:
        record("Юнит-тесты", "SKIP", "тестов ещё нет")
        return
    args = [PY, "-m", "pytest", "-q", *existing]
    code, tail = run(args, cwd=BACKEND)
    record("Юнит-тесты", "PASS" if code == 0 else "FAIL", tail)


# ── Шаг 4. Полная приёмка A01–A26 ────────────────────────────────────────
def step_acceptance() -> tuple[int, int]:
    script = BACKEND / "scripts" / "acceptance_claim.py"
    if not script.exists():
        record("Приёмка A01–A26", "SKIP", "acceptance_claim.py ещё нет")
        return 0, 0
    code, tail = run([PY, str(script)], cwd=BACKEND)
    passed = total = 0
    for line in tail.split(" | "):
        if "PASS" in line:
            passed += 1
    # точный счёт читаем из последней строки вида «PASS NN/26»
    try:
        out = subprocess.run(
            [PY, str(script)], cwd=str(BACKEND), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=300,
        ).stdout
        for line in out.splitlines():
            if line.strip().startswith("PASS "):
                num, _, tot = line.strip().split(" ")[1].partition("/")
                passed, total = int(num), int(tot)
    except Exception:  # noqa: BLE001
        pass
    status = "PASS" if code == 0 else ("FAIL" if total else "SKIP")
    record("Приёмка A01–A26", status, f"{passed}/{total}")
    return passed, total


# ── Шаг 5. Линтеры ───────────────────────────────────────────────────────
def step_linters() -> None:
    for tool, args in (
        ("ruff", [PY, "-m", "ruff", "check", "app"]),
        ("mypy --strict", [PY, "-m", "mypy", "app"]),
    ):
        code, tail = run(args, cwd=BACKEND)
        if code == 127:
            record(f"Линтер {tool}", "SKIP", "не установлен")
        else:
            record(f"Линтер {tool}", "PASS" if code == 0 else "FAIL", tail)


# ── Шаг 6. Замер времени генерации ───────────────────────────────────────
def step_timing() -> None:
    script = BACKEND / "scripts" / "measure_claim_time.py"
    if not script.exists():
        record("Замер времени рендера (<1 с)", "SKIP", "скрипта ещё нет")
        return
    code, tail = run([PY, str(script)], cwd=BACKEND)
    record("Замер времени рендера (<1 с)", "PASS" if code == 0 else "FAIL", tail)


# ── Шаг 7. Отчёт ─────────────────────────────────────────────────────────
def write_report(passed: int, total: int) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Прогресс приёмки: дело 2-101/2025",
        "",
        f"_Обновлено: {time.strftime('%Y-%m-%d %H:%M')}_",
        "",
        f"**Счёт приёмки: {passed}/{total}**",
        "",
        "| Шаг | Статус | Детали |",
        "|---|---|---|",
    ]
    for step, status, detail in RESULTS:
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⬜"}.get(status, "•")
        lines.append(f"| {step} | {icon} {status} | {detail} |")
    lines += [
        "",
        "## Целевые отметки по коммитам",
        "",
        "| Коммит | Ожидаемый счёт | Факт |",
        "|---|---|---|",
        "| 0 фикстуры | ≥1/26 | |",
        "| 1 канон денег | ≥6/26 | |",
        "| 2 паспорт+модель | ≥9/26 | |",
        "| 3 рендер | ≥22/26 | |",
        "| 4 верификация+гейт | ≥25/26 | |",
        "| 5 разделение+фронтенд | 26/26 | |",
        "",
    ]
    (DOCS / "acceptance-progress.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nОтчёт: {DOCS / 'acceptance-progress.md'}")


def main() -> int:
    print("=" * 62)
    print("ЕДИНЫЙ ПРОГОН ПРИЁМКИ  (Файл 8)")
    print("=" * 62)
    if not step_prereq():
        write_report(0, 0)
        return 2
    step_docx()
    step_modules()
    step_unit_tests()
    passed, total = step_acceptance()
    step_linters()
    step_timing()
    write_report(passed, total)
    failed = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print("-" * 62)
    print(f"ИТОГ: приёмка {passed}/{total}, проваленных шагов {failed}")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
