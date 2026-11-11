#!/usr/bin/env python3
"""Единая оценка CourtDraft AI: e2e и пораздельный разбор.

e2e   — полный проект по материалам дела;
blocks — тот же прогон, но отчёт по разделам и их пригодности.

Бюджет для openai режется TrackingProvider: до платного вызова, если
лимит уже не позволяет следующий запрос.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_deepseek_quality import (  # noqa: E402
    evaluate_case,
    fetch_prices,
    make_provider,
)

from app.agents.evidence_pack import load_template  # noqa: E402
from app.config import LLMProviderName, Settings  # noqa: E402
from app.llm import build_llm_provider  # noqa: E402

DEFAULT_CASES = ROOT / "artifacts" / "cd" / "synth" / "cases"
DEFAULT_OUT = ROOT / "artifacts" / "cd" / "eval"


class CostlessProvider:
    """Обёртка локальной/mock-модели: расход API равен нулю."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self.spent_rub = 0.0
        self.current_case = ""
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return getattr(self._provider, "name", "local")

    async def generate(self, request: Any) -> Any:
        response = await self._provider.generate(request)
        self.calls.append({"case": self.current_case, "model": getattr(response, "model", "")})
        return response

    async def generate_json(
        self, request: Any, *, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._provider.generate_json(request, default=default)

    async def health(self) -> bool:
        return await self._provider.health()


def _settings(provider: str, model: str, max_tokens: int) -> Settings:
    base = Settings()
    update: dict[str, Any] = {
        "llm_provider": LLMProviderName(provider),
        "embedding_provider": "mock",
        "response_cache_enabled": False,
        "openai_retries": 0,
    }
    if provider == "ollama":
        update["ollama_enabled"] = True
        update["ollama_model"] = model or base.ollama_model
    if provider == "openai" and model:
        update["openai_model"] = model
        update["openai_model_auto"] = False
    if max_tokens:
        update["openai_max_tokens"] = max_tokens
    return base.model_copy(update=update)


def _write_report(out: Path, items: list[Any], *, mode: str, provider: str, spent: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "provider": provider,
        "spent_rub": round(spent, 6),
        "cases": len(items),
        "errors": sum(1 for item in items if item.error),
        "p0": sum(1 for item in items if item.p0),
        "items": [item.__dict__ for item in items],
    }
    (out / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        f"# CD eval: {mode}",
        "",
        f"- Провайдер: `{provider}`",
        f"- Дел: {len(items)}",
        f"- Ошибок: {payload['errors']}",
        f"- P0: {payload['p0']}",
        f"- Расход: {spent:.4f} ₽",
        "",
        "| Дело | Разделов | P0 | Ошибка |",
        "|---|---:|---:|---|",
    ]
    for item in items:
        lines.append(
            f"| {item.case} | {len(item.sections)} | {len(item.p0)} | {item.error[:120]} |"
        )
    out.joinpath("report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    cases_dir = Path(args.cases)
    if not cases_dir.exists():  # noqa: ASYNC240
        print(f"нет каталога дел: {cases_dir}")
        return 2
    cases = sorted(
        path
        for path in cases_dir.iterdir()  # noqa: ASYNC240
        if path.is_dir() and (path / "court_reference.txt").exists()
    )
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("нет дел с court_reference.txt")
        return 1

    settings = _settings(args.provider, args.model, args.max_tokens)
    template = load_template(settings)
    if args.provider == "openai":
        prices, error = fetch_prices(settings)
        if error:
            print(f"прайс: {error}")
        provider: Any = make_provider(
            settings, budget_rub=args.budget_rub, prices=prices
        )
        ok = await provider.health()
        print(f"openai health: {ok}; бюджет {args.budget_rub:.2f} ₽")
        if not ok:
            return 2
    else:
        provider = CostlessProvider(build_llm_provider(settings))
        print(f"{args.provider}: {getattr(provider, 'name', 'local')}")

    items: list[Any] = []
    out = Path(args.out)
    for index, case_dir in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case_dir.name}")
        item = await evaluate_case(
            case_dir,
            settings=settings,
            provider=provider,
            template=template,
        )
        items.append(item)
        if item.error.startswith("BUDGET"):
            print("останов: бюджет")
            break

    spent = float(getattr(provider, "spent_rub", 0.0))
    _write_report(out, items, mode=args.mode, provider=args.provider, spent=spent)
    print(f"отчёт: {out / 'report.md'}")
    print(f"расход: {spent:.4f} ₽")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["e2e", "blocks"], default="e2e")
    parser.add_argument("--provider", choices=["mock", "ollama", "openai"], default="mock")
    parser.add_argument("--model", default="")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=0)
    parser.add_argument("--budget-rub", type=float, default=2.0)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
