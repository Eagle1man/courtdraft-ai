"""Проверка повторной проверки в интерфейсе (Этап 8).

Сценарий, который проходит судья:

1. проект проверен — отчёт действующий;
2. судья вносит правку в текст (и добавляет сумму, которой нет в деле);
3. старый отчёт помечается как относящийся к прежней версии — и не
   продолжает выглядеть действующим;
4. «Проверить заново» считает отчёт по текущему тексту, и выдуманная
   сумма не даёт статуса «проверено».

Запуск (демо-стенд поднят, Playwright в системном Python):

    python scripts/ui_reverify_check.py
    python scripts/ui_reverify_check.py --url http://127.0.0.1:8000/app

Браузер — установленный Edge через channel="msedge". Демо-база отдельная:
сценарий добавляет версию, рабочая БД не затрагивается.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

DEFAULT_URL = "http://127.0.0.1:8000/app"
FABRICATED_AMOUNT = " Взыскать с ответчика 999 999 руб."


def verdict(page) -> str:
    return page.locator("#verdict").inner_text()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--screenshot", default="artifacts/ui-reverify.png")
    args = parser.parse_args()

    failures: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
        except PlaywrightError as exc:
            print(f"Не удалось запустить Edge через Playwright: {exc}")
            return 2

        page = browser.new_page(viewport={"width": 1440, "height": 1200})
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            and not message.text.startswith("Failed to load resource")
            else None,
        )
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#sections .sec", timeout=60000)
            page.wait_for_selector("#verification", state="visible", timeout=60000)
            before = verdict(page)

            page.locator("#editor summary").click()
            page.wait_for_selector("#edit-text", state="visible", timeout=20000)
            text = page.locator("#edit-text").input_value()
            page.locator("#edit-text").fill(text + FABRICATED_AMOUNT)
            page.locator("#btn-save-edit").click()

            # После сохранения версии старый отчёт обязан стать устаревшим.
            page.wait_for_selector(
                "#verification.stale", timeout=60000
            )
            stale_text = verdict(page)

            page.locator("#btn-reverify").click()
            page.wait_for_selector(
                "#verification:not(.stale)", timeout=60000
            )
            page.wait_for_function(
                "() => !document.querySelector('#verification').classList.contains('stale')",
                timeout=60000,
            )
            after = verdict(page)

            screenshot = Path(args.screenshot)
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)
        except PlaywrightError as exc:
            print(f"Сценарий не дошёл до конца: {exc}")
            try:
                page.screenshot(path=args.screenshot, full_page=True)
                print(f"Снимок состояния: {args.screenshot}")
            except PlaywrightError:
                pass
            return 3
        finally:
            browser.close()

    if "прежней версии" not in stale_text:
        failures.append("после правки отчёт не помечен как относящийся к прежней версии")
    if "прежней версии" in after:
        failures.append("после повторной проверки пометка об устаревшем отчёте осталась")
    if after == stale_text:
        failures.append("повторная проверка не изменила вердикт — отчёт не пересчитан")
    if after == before:
        failures.append("вердикт совпал с исходным: правка не повлияла на проверку")
    if console_errors:
        failures.append("ошибки в консоли: " + "; ".join(console_errors[:5]))

    summary = {
        "url": args.url,
        "before": before.strip()[:160],
        "stale_after_edit": stale_text.strip()[:160],
        "after_reverify": after.strip()[:160],
        "screenshot": args.screenshot,
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        print("НЕ ПРОШЛО")
        return 1
    print(
        "ПРОШЛО: правка делает отчёт устаревшим, повторная проверка считает его заново"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
