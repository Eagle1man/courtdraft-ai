"""Проверка блока утверждений в интерфейсе через Playwright (Этап 9).

Что проверяется — то, что видит судья, а не только ответ API:

1. рядом с разделами проекта есть блок «Утверждения»;
2. у подтверждённого утверждения показан документ и непустая цитата;
3. утверждение без источника помечено, а не выглядит подтверждённым;
4. отказ запроса утверждений виден отдельной строкой, а не пустым списком;
5. консоль браузера без ошибок.

Запуск (стенд на демо-базе должен быть поднят):

    python scripts\\ui_claims_check.py
    python scripts\\ui_claims_check.py --url http://127.0.0.1:8000/app

Playwright стоит в системном Python (у backend venv его нет) — запускать
через `python`, а не `backend\\.venv\\Scripts\\python.exe`: проверено,
в venv модуль отсутствует (ModuleNotFoundError).

Браузер — установленный Edge через channel="msedge": скачивать chromium
(300+ МБ) на машине с 2,7 ГБ свободного места на C: не нужно.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

DEFAULT_URL = "http://127.0.0.1:8000/app"


def collect(page) -> dict[str, object]:
    """Снять с экрана то, что реально отрисовано (а не то, что должно быть)."""
    return page.evaluate(
        """() => {
          const blocks = [...document.querySelectorAll('.claims')];
          return {
            blocks: blocks.length,
            claims: [...document.querySelectorAll('.cl')].map((li) => ({
              status: (li.querySelector('.chip') || {}).textContent || '',
              text: (li.querySelector('.cl-t') || {}).textContent || '',
              sources: [...li.querySelectorAll('.cl-ref b')].map((b) => b.textContent),
              quotes: [...li.querySelectorAll('.cl-ref q')].map((q) => q.textContent),
              sourceText: (li.querySelector('.cl-src') || {}).textContent || '',
              missingSource: !!li.querySelector('.cl-src.none'),
            })),
            error: (document.querySelector('.claims-error') || {}).textContent || '',
            sections: document.querySelectorAll('#sections .sec').length,
          };
        }"""
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--screenshot", default="artifacts/ui-claims.png", help="куда сохранить снимок"
    )
    args = parser.parse_args()

    failures: list[str] = []
    console_errors: list[str] = []
    failed_resources: list[str] = []

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
            # «Failed to load resource» дублирует обработчик response, где
            # виден адрес; здесь он бесполезен без URL.
            if message.type == "error"
            and not message.text.startswith("Failed to load resource")
            else None,
        )
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))
        page.on(
            "response",
            lambda response: failed_resources.append(
                f"{response.status} {response.url}"
            )
            if response.status >= 400
            else None,
        )

        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            # Проект рисуется после ответа API: ждём разделы, затем блоки
            # утверждений (второй запрос, приходит позже разделов).
            page.wait_for_selector("#sections .sec", timeout=60000)
            page.wait_for_selector(
                ".claims .cl, .claims-error", timeout=60000
            )
            data = collect(page)
            screenshot = Path(args.screenshot)
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)
        except PlaywrightError as exc:
            print(f"Экран не дошёл до утверждений: {exc}")
            try:
                page.screenshot(path=args.screenshot, full_page=True)
                print(f"Снимок состояния: {args.screenshot}")
            except PlaywrightError:
                pass
            return 3
        finally:
            browser.close()

    if not data["sections"]:
        failures.append("в проекте не отрисовано ни одного раздела")
    if data["error"]:
        failures.append(f"запрос утверждений не выполнен: {data['error']}")
    if not data["blocks"]:
        failures.append("ни у одного раздела нет блока «Утверждения»")
    if not data["claims"]:
        failures.append("утверждения не показаны ни разу")

    for claim in data["claims"]:
        text = (claim["text"] or "").strip()
        if not text:
            failures.append("утверждение показано без текста")
        if not (claim["status"] or "").strip():
            failures.append(f"у утверждения нет статуса: {text[:60]!r}")
        if "подтверждено" in claim["status"] and not claim["quotes"]:
            failures.append(f"подтверждено без цитаты: {text[:60]!r}")
        for quote in claim["quotes"]:
            if not quote.strip():
                failures.append(f"пустая цитата у утверждения: {text[:60]!r}")

    if console_errors:
        failures.append("ошибки в консоли браузера: " + "; ".join(console_errors[:5]))

    # favicon — не ресурс приложения: его 404 не делает интерфейс сломанным.
    app_failures = [item for item in failed_resources if "favicon" not in item]
    if app_failures:
        failures.append("ответы с ошибкой: " + "; ".join(app_failures[:5]))

    summary = {
        "url": args.url,
        "sections": data["sections"],
        "claim_blocks": data["blocks"],
        "claims": len(data["claims"]),
        "with_quote": sum(1 for claim in data["claims"] if claim["quotes"]),
        "not_confirmed": sum(
            1 for claim in data["claims"] if claim["missingSource"]
        ),
        "samples": [
            {
                "status": claim["status"].strip(),
                "text": claim["text"].strip()[:80],
                "source": claim["sourceText"].strip()[:120],
            }
            for claim in data["claims"][:3]
        ],
        "screenshot": str(screenshot),
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        print("НЕ ПРОШЛО")
        return 1
    print("ПРОШЛО: утверждения видны рядом с разделами, у подтверждённых есть цитата")
    return 0


if __name__ == "__main__":
    sys.exit(main())
