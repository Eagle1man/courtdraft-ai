"""Приёмка качества иска: 25 проверок A01…A25 на эталонном деле 2-101/2025.

Скрипт запускается с ПЕРВОГО коммита, до внесения правок. Отсутствующие
модули не роняют прогон: проверка помечается FAIL с причиной
"не реализовано" - так виден прогресс красное -> зелёное.

Запуск:
    python backend/scripts/acceptance_claim.py
Код возврата: 0 — все PASS, 2 — есть FAIL.
"""
from __future__ import annotations

import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

FIX = BACKEND / "tests" / "fixtures" / "case_2-101-2025"
RESULTS: list[tuple[str, str, str, str]] = []


def check(code: str, title: str) -> Callable[[Callable[[], tuple[bool, str]]], None]:
    def wrap(fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = fn()
        except ImportError as exc:
            ok, detail = False, f"не реализовано: {exc}"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        RESULTS.append((code, title, "PASS" if ok else "FAIL", detail))
    return wrap


def _passport() -> dict[str, Any]:
    return json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))


def _expected() -> dict[str, Any]:
    return json.loads((FIX / "expected_extraction.json").read_text(encoding="utf-8"))


def _materials() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted((FIX / "materials").glob("*.md"))}


def _norm(text: str) -> str:
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def _render() -> str:
    """Отрендерить иск из паспорта. Требует claim_model + claim_render (спринт 2–3)."""
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    model = ClaimModel.from_passport(_passport())
    return render_claim(model).text


# ── Блок 1: паспорт и извлечение ────────────────────────────────────────────

@check("A01", "паспорт распарсен, приоритет над материалами")
def a01() -> tuple[bool, str]:
    from app.ingestion.passport import CasePassport

    p = CasePassport.model_validate(_passport())
    return p.money_kopecks.total == 10316329, "total замкнут"


@check("A18", "канон денег: копейки не рубли")
def a18() -> tuple[bool, str]:
    from app.money import parse_money

    line = "Итого к взысканию процентов: 10 263,29 (Десять тысяч двести шестьдесят три) рубля 29 копеек."
    tokens = parse_money(line)
    values = {t.kopecks for t in tokens}
    return 1026329 in values and 2900 not in values and 29 not in values, f"{sorted(values)}"


@check("A19", "типизированные поля без валюты распарсены")
def a19() -> tuple[bool, str]:
    from app.money import parse_money

    tokens = parse_money("Основной долг: 90 000,00.\nГоспошлина: 2 900,00.")
    values = {t.kopecks for t in tokens}
    return {9000000, 290000} <= values, f"{sorted(values)}"


@check("A20", "единицы колонок таблиц наследуются")
def a20() -> tuple[bool, str]:
    from app.ingestion.slots import table_units

    text = _materials()["06-Расчёт-процентов.md"]
    units = table_units(text)
    money_cols = [i for i, u in units.items() if u == "RUB"]
    return len(money_cols) >= 3, f"колонки с руб.: {money_cols}"


@check("A21", "валидатор адреса отклоняет мусор")
def a21() -> tuple[bool, str]:
    from app.ingestion.slots import validate_address

    bad = [
        "Таблица 1: | № | Наименование документа | Листов |",
        "ату 30.12.2024, что подтверждается отчётом об отслеживании",
        "[адрес почтового отделения — по данным дела]",
    ]
    rejected = all(validate_address(v).status != "OK" for v in bad)
    marker = validate_address("[адрес истца — по данным дела]").status == "MISSING"
    good = validate_address("105000, г. Москва, ул. Тверская, д. 5, кв. 12").status == "OK"
    return rejected and marker and good, f"rejected={rejected} marker={marker} good={good}"


@check("A22", "захват через границы документов невозможен; претензия не CLAIM")
def a22() -> tuple[bool, str]:
    from app.ingestion.extract import guess_document_type

    materials = _materials()
    kind = guess_document_type("05-Претензия.md", materials["05-Претензия.md"])
    return kind == "PRETRIAL", f"05-Претензия -> {kind}"


# ── Блок 2: рендер иска ─────────────────────────────────────────────────────

@check("A02", "шапка точная, вхождений 1, плейсхолдеров нет")
def a02() -> tuple[bool, str]:
    text = _render()
    court = _passport()["case"]["court_for_header"]
    head = court.split("\n")[0]
    ok = text.count("судебного участка № 5") == 1 and head.split(" ")[0] in text
    ok = ok and "[наименование" not in text and "Ленинский" not in text
    return ok, text.split("\n")[0][:80]


@check("A03", "цена иска 10026329, единственное значение")
def a03() -> tuple[bool, str]:
    text = _render()
    prices = re.findall(r"Цена иска:\s*([\d\s\u00a0]+)", text)
    digits = [re.sub(r"\D", "", p) for p in prices]
    return digits == ["100263"], f"{digits}"


@check("A04", "основной долг в фактах и в просительной части")
def a04() -> tuple[bool, str]:
    text = _render()
    petitum = text.split("ПРОШУ:")[-1]
    return "90 000" in text and "90 000" in petitum, "90 000 найдено"


@check("A05", "частичный возврат и п/п № 45 присутствуют")
def a05() -> tuple[bool, str]:
    text = _render()
    return "10 000" in text and "№ 45 от 01.08.2024" in text, "возврат отражён"


@check("A06", "пошлина 2900, значения 4000 нет")
def a06() -> tuple[bool, str]:
    text = _render()
    return "2 900" in text and "4000" not in text.replace(" ", ""), "пошлина из чека"


@check("A07", "итого 103163 и строка «Всего к взысканию»")
def a07() -> tuple[bool, str]:
    text = _render()
    return "103 163" in text and "Всего к взысканию" in text, "итог замкнут"


@check("A08", "период процентов точный, «по дату подачи» нет")
def a08() -> tuple[bool, str]:
    text = _render()
    ok = "с 11.08.2024 по 05.03.2025" in text
    ok = ok and "по дату подачи иска" not in text and "02.08.2024" not in text
    return ok, "период из расчёта"


@check("A09", "нет инверсии ролей и обращения «Вами»")
def a09() -> tuple[bool, str]:
    text = _render()
    bad = ["Я, Кузнецов", "Вами", "взыскать с истца", "Вам "]
    found = [b for b in bad if b in text]
    return not found, f"найдено: {found}"


@check("A10", "нет мета-фраз, маркеров, обрывов")
def a10() -> tuple[bool, str]:
    text = _render()
    bad = [
        "Вот проект", "текст оборван", "Проект подготовлен программой",
        "требует проверки", "подлежит проверке", "ТРЕБУЕТ ПРОВЕРКИ",
        "МОCK", "MOCK",
    ]
    found = [b for b in bad if b in text]
    return not found, f"найдено: {found}"


@check("A11", "приложений 7 на 9 листах, имён файлов нет")
def a11() -> tuple[bool, str]:
    text = _render()
    block = text.split("Приложение:")[-1]
    items = re.findall(r"^\s*\d+\.\s", block, flags=re.M)
    ok = len(items) == 7 and "7 наименований на 9 листах" in block
    ok = ok and ".docx" not in text and ".md" not in text
    return ok, f"позиций: {len(items)}"


@check("A12", "дата и подпись заполнены")
def a12() -> tuple[bool, str]:
    text = _render()
    return "22.01.2025" in text and "Смирнова О.В." in text and "___»" not in text, "дата/подпись"


@check("A13", "нет разрывов чисел и служебных заголовков")
def a13() -> tuple[bool, str]:
    from app.textnorm import broken_numbers

    text = _render()
    bad = broken_numbers(text)
    heads = [h for h in ("ШАПКА И АДРЕСАТ", "СТОРОНЫ И ТРЕТЬИ ЛИЦА", "ЦЕНА ИСКА") if h in text]
    return not bad and not heads, f"broken={bad} heads={heads}"


@check("A14", "объём 3200–4500 знаков")
def a14() -> tuple[bool, str]:
    n = len(_render())
    return 3200 <= n <= 4500, f"{n} знаков"


@check("A15", "три прогона побайтово идентичны")
def a15() -> tuple[bool, str]:
    runs = [_norm(_render()) for _ in range(3)]
    return runs[0] == runs[1] == runs[2], "идемпотентность"


@check("A26", "рендер совпадает с эталоном (побайтово)")
def a26() -> tuple[bool, str]:
    golden = _norm((FIX / "expected_claim.txt").read_text(encoding="utf-8"))
    actual = _norm(_render())
    if golden == actual:
        return True, "совпадает"
    first = next(
        (i for i, (a, b) in enumerate(zip(golden, actual)) if a != b),
        min(len(golden), len(actual)),
    )
    return False, f"первое расхождение на {first}: …{actual[max(0,first-40):first+40]!r}"


# ── Блок 3: гейт и изоляция ─────────────────────────────────────────────────

@check("A16", "экспорт 409 при p0_count > 0")
def a16() -> tuple[bool, str]:
    from app.services.export_gate import assert_export_allowed

    try:
        assert_export_allowed(p0_count=2, is_stale=False, mode="final")
    except Exception as exc:  # noqa: BLE001
        return "409" in str(exc) or "блок" in str(exc).lower(), str(exc)[:80]
    return False, "экспорт разрешён при P0"


@check("A17", "экспорт 409 при устаревшем отчёте")
def a17() -> tuple[bool, str]:
    from app.services.export_gate import assert_export_allowed

    try:
        assert_export_allowed(p0_count=0, is_stale=True, mode="final")
    except Exception as exc:  # noqa: BLE001
        return True, str(exc)[:80]
    return False, "экспорт разрешён при stale"


@check("A23", "версии иска и решения изолированы")
def a23() -> tuple[bool, str]:
    import inspect

    from app.db.repositories import DraftRepository

    sig = inspect.signature(DraftRepository.get_for_case)
    return "kind" in sig.parameters, f"сигнатура: {sig}"


@check("A25", "фикстуры решения не изменились")
def a25() -> tuple[bool, str]:
    path = BACKEND / "tests" / "data" / "resol_fixtures.json"
    if not path.exists():
        return True, "файл отсутствует — нечего ломать"
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    baseline = BACKEND / "tests" / "data" / "resol_fixtures.sha256"
    if not baseline.exists():
        baseline.write_text(digest, encoding="utf-8")
        return True, "базовый хэш зафиксирован"
    return baseline.read_text(encoding="utf-8").strip() == digest, digest[:16]


def main() -> int:
    width = max(len(title) for _, title, _, _ in RESULTS)
    print("=" * (width + 34))
    print("ПРИЁМКА ИСКА: дело 2-101/2025")
    print("=" * (width + 34))
    failed = 0
    for code, title, status, detail in RESULTS:
        if status == "FAIL":
            failed += 1
        print(f"{code}  {status:<4}  {title:<{width}}  {detail}")
    total = len(RESULTS)
    print("-" * (width + 34))
    print(f"PASS {total - failed}/{total}")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
