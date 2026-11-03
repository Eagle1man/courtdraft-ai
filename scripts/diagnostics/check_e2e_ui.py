"""Браузерный тест интерфейса: два участника на одном стенде.

Сценарий как в жизни: S01 вводит код, создаёт дело и грузит файл; S02 в
чистом профиле браузера видит пустой список, создаёт своё дело и грузит свой
файл; проверяется, что материалы не смешались и смена кода работает.

Нужен Playwright (`pip install playwright`, браузер — Edge или Chrome из
системы). Запуск:

    python scripts\\diagnostics\\check_e2e_ui.py
    set COURTDRAFT_URL=http://127.0.0.1:8081 && python ...
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

import requests
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(errors="replace")

HOST = os.environ.get("COURTDRAFT_URL", "http://10.19.20.20:8081").rstrip("/")
BASE = HOST + "/app"
API = HOST
TMP = pathlib.Path(tempfile.mkdtemp(prefix="courtdraft_e2e_"))
stamp = str(int(time.time()))[-6:]

file_a = TMP / f"isk_S01_{stamp}.txt"
file_b = TMP / f"dogovor_S02_{stamp}.txt"
file_a.write_text(
    "ИСКОВОЕ ЗАЯВЛЕНИЕ\nВзыскать с ООО «Ромашка» 150 000 руб. долга.\n",
    encoding="utf-8",
)
file_b.write_text(
    "ДОГОВОР ПОСТАВКИ № 7\nПоставщик обязуется поставить товар на 90 000 руб.\n",
    encoding="utf-8",
)

created: list[str] = []


def cleanup() -> None:
    _, data = 200, requests.get(f"{API}/api/cases", timeout=20).json()
    for item in data.get("items", []):
        if item["case_number"].startswith(f"E2E-{stamp}"):
            requests.delete(f"{API}/api/cases/{item['id']}", timeout=20)
    print("уборка выполнена")


def wait_list(page) -> None:
    page.wait_for_function(
        """() => {
            const el = document.getElementById('case-list');
            return el && !el.textContent.includes('Загрузка');
        }""",
        timeout=20000,
    )


def enter_code(page, code: str) -> None:
    page.wait_for_selector("#student-gate:not([hidden])", timeout=20000)
    page.fill("#gate-student", code)
    page.click("#gate-submit")
    page.wait_for_selector("#student-gate[hidden]", state="attached", timeout=20000)
    wait_list(page)


def create_case(page, number: str) -> None:
    page.click("#btn-new-case")
    page.fill("#case-number", number)
    page.fill("#case-court", "Проверочный районный суд")
    page.click("#case-form button[type=submit]")
    page.wait_for_selector(f"#case-list .case:has-text('{number}')", timeout=20000)


def upload(page, path: pathlib.Path) -> None:
    page.set_input_files("#file-input", str(path))
    page.wait_for_selector(f"#doc-list .doc:has-text('{path.name}')", timeout=60000)


def docs(page) -> list[str]:
    return page.eval_on_selector_all(
        "#doc-list .doc .nm", "els => els.map(e => e.textContent.trim())"
    )


def cases(page) -> list[str]:
    return page.eval_on_selector_all(
        "#case-list .case .num", "els => els.map(e => e.textContent.trim())"
    )


try:
    with sync_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome"):
            try:
                browser = p.chromium.launch(channel=channel)
                print("браузер:", channel)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"{channel} недоступен: {type(exc).__name__}")
        if browser is None:
            browser = p.chromium.launch()
            print("браузер: встроенный chromium")

        # ── Участник S01 ──────────────────────────────────────────
        ctx1 = browser.new_context()
        page1 = ctx1.new_page()
        page1.goto(BASE, wait_until="domcontentloaded")
        enter_code(page1, "S01")
        print("S01: список после ввода кода:", cases(page1))
        assert not any(c.startswith(f"E2E-{stamp}") for c in cases(page1))

        create_case(page1, f"E2E-{stamp}-A")
        page1.wait_for_function(
            "() => document.getElementById('btn-generate').disabled === true",
            timeout=20000,
        )
        upload(page1, file_a)
        print("S01: материалы:", docs(page1))
        assert docs(page1) == [file_a.name], docs(page1)

        # Код в шапке сменился на S01
        badge = page1.text_content("#student-badge").strip()
        print("S01: код в шапке:", badge)
        assert badge == "S01"

        # ── Участник S02 (чистый профиль) ─────────────────────────
        ctx2 = browser.new_context()
        page2 = ctx2.new_page()
        page2.goto(BASE, wait_until="domcontentloaded")
        enter_code(page2, "S02")
        print("S02: список после ввода кода:", cases(page2))
        assert not any(c.startswith(f"E2E-{stamp}") for c in cases(page2))

        create_case(page2, f"E2E-{stamp}-B")
        upload(page2, file_b)
        print("S02: материалы:", docs(page2))
        assert docs(page2) == [file_b.name], docs(page2)

        # Возвращаемся к S01: у него по-прежнему только своё
        page1.reload(wait_until="domcontentloaded")
        wait_list(page1)
        # Список дел появляется раньше, чем подгружаются материалы
        # выбранного дела — ждём именно материалы.
        page1.wait_for_selector(
            f"#doc-list .doc:has-text('{file_a.name}')", timeout=20000
        )
        print("S01 после перезагрузки: дела:", cases(page1))
        print("S01 после перезагрузки: материалы:", docs(page1))
        assert cases(page1) == [f"E2E-{stamp}-A"], cases(page1)
        assert docs(page1) == [file_a.name], docs(page1)
        assert file_b.name not in docs(page1)

        # Смена кода в шапке: S01 -> S02 на том же профиле
        page1.click("#btn-student")
        page1.wait_for_selector("#student-gate:not([hidden])", timeout=10000)
        page1.fill("#gate-student", "S02")
        page1.click("#gate-submit")
        page1.wait_for_selector("#student-gate[hidden]", state="attached", timeout=20000)
        wait_list(page1)
        page1.wait_for_selector(
            f"#doc-list .doc:has-text('{file_b.name}')", timeout=20000
        )
        print("после смены кода на S02: дела:", cases(page1))
        print("после смены кода на S02: материалы:", docs(page1))
        assert cases(page1) == [f"E2E-{stamp}-B"], cases(page1)
        assert docs(page1) == [file_b.name], docs(page1)

        browser.close()

    print("OK: интерфейс разделяет участников, материалы не смешиваются")
finally:
    cleanup()
