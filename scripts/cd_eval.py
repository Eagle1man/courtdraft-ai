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
import os
import random
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from cd_core import amounts_kop  # noqa: E402
from eval_deepseek_quality import (  # noqa: E402
    evaluate_case,
    fetch_prices,
    make_provider,
)

from app.agents.evidence_pack import load_template  # noqa: E402
from app.config import LLMProviderName, Settings  # noqa: E402
from app.llm import build_llm_provider  # noqa: E402
from app.llm.base import LLMMessage, LLMRequest, LLMRole  # noqa: E402

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


def _tokens(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]{3,}", (text or "").lower())


def _token_f1(reference: str, generated: str) -> float:
    ref = Counter(_tokens(reference))
    got = Counter(_tokens(generated))
    if not ref or not got:
        return 0.0
    overlap = sum((ref & got).values())
    precision = overlap / sum(got.values())
    recall = overlap / sum(ref.values())
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _sequence_ratio(reference: str, generated: str) -> float:
    return SequenceMatcher(None, (reference or "").lower(), (generated or "").lower()).ratio()


def _citation_f1(reference: str, generated: str) -> float:
    from cd_core import citations

    ref = citations(reference)
    got = citations(generated)
    if not ref or not got:
        return 0.0
    overlap = len(ref & got)
    precision = overlap / len(got)
    recall = overlap / len(ref)
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _amount_f1(reference: str, generated: str) -> float:
    from cd_core import amounts_kop

    ref = set(amounts_kop(reference))
    got = set(amounts_kop(generated))
    if not ref or not got:
        return 0.0
    overlap = len(ref & got)
    precision = overlap / len(got)
    recall = overlap / len(ref)
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _task_prompt(task: dict[str, Any]) -> str:
    labels = {
        "requirements": "требования истца",
        "plaintiff_arguments": "аргументы истца",
        "defendant_arguments": "аргументы ответчика",
        "evidence_assessment": "оценка судом доказательств",
        "judge_logic": "логика рассуждений судьи",
        "norms": "применимые нормы права",
        "decision": "краткое содержание решения суда",
    }
    parts = [
        "Ты — помощник судьи. По фрагментам судебного акта определи "
        f"только блок «{labels.get(task['target'], task['target'])}».",
        "Не добавляй факты, суммы и нормы, которых нет во входных данных.",
        "Верни только текст этого блока, без пояснений и заголовков.",
        "",
        "ВХОД:",
    ]
    for name, text in task.get("input", {}).items():
        parts.append(f"[{name}]")
        parts.append(str(text))
        parts.append("")
    parts.append("ОТВЕТ:")
    return "\n".join(parts)


async def _run_blocks(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():  # noqa: ASYNC240
        print(f"нет файла заданий: {tasks_path}")
        return 2
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))  # noqa: ASYNC240
    if args.branches:
        wanted = [item.strip().lower() for item in args.branches.split(",") if item.strip()]
        tasks = [
            task for task in tasks
            if any(branch in str(task.get("category", "")).lower() for branch in wanted)
        ]
    if args.targets:
        wanted_targets = {item.strip() for item in args.targets.split(",") if item.strip()}
        tasks = [task for task in tasks if task.get("target") in wanted_targets]
    if args.sample:
        random.Random(args.seed).shuffle(tasks)  # noqa: S311 - выборка, не криптография
        tasks = tasks[: args.sample]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("нет заданий после фильтра")
        return 1

    settings = _settings(args.provider, args.model, args.max_tokens)
    if args.provider == "openai" and os.environ.get(
        "ALLOW_CLOUD_LLM", ""
    ).lower() not in {"1", "true", "yes"}:
        print("Облачный провайдер отключён. Для включения: ALLOW_CLOUD_LLM=true.")
        return 2
    if args.provider == "openai":
        prices, error = fetch_prices(settings)
        if error:
            print(f"прайс: {error}")
        provider: Any = make_provider(settings, budget_rub=args.budget_rub, prices=prices)
        if not await provider.health():
            print("openai недоступен")
            return 2
    else:
        provider = CostlessProvider(build_llm_provider(settings))
    print(f"blocks: {len(tasks)} заданий, провайдер {args.provider}")

    results: list[dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        print(f"[{index}/{len(tasks)}] {task['case']} / {task['target']}")
        try:
            response = await provider.generate(
                LLMRequest(
                    messages=[LLMMessage(role="user", content=_task_prompt(task))],
                    role=LLMRole.GENERATION,
                    temperature=0.1,
                    max_tokens=args.max_tokens or 700,
                )
            )
            generated = response.text
            error = ""
        except Exception as exc:  # noqa: BLE001
            generated = ""
            error = f"{type(exc).__name__}: {exc}"
            if "бюджет" in error.lower():
                results.append({**task, "generated": "", "error": error, "score": 0.0})
                break
        reference = str(task.get("reference") or "")
        score = _token_f1(reference, generated)
        results.append(
            {
                **task,
                "generated": generated,
                "error": error,
                "score": round(score, 4),
                "sequence": round(_sequence_ratio(reference, generated), 4),
                "citation_f1": round(_citation_f1(reference, generated), 4),
                "amount_f1": round(_amount_f1(reference, generated), 4),
            }
        )

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    payload = {
        "mode": "blocks",
        "provider": args.provider,
        "tasks": len(results),
        "spent_rub": round(float(getattr(provider, "spent_rub", 0.0)), 6),
        "mean_score": round(
            sum(item["score"] for item in results) / max(1, len(results)), 4
        ),
        "results": results,
    }
    (out / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    by_target: dict[str, list[float]] = defaultdict(list)
    for item in results:
        by_target[str(item["target"])].append(float(item["score"]))
    lines = [
        "# CD eval: blocks",
        "",
        f"- Провайдер: `{args.provider}`",
        f"- Заданий: {len(results)}",
        f"- Средний token-F1: {payload['mean_score']:.4f}",
        f"- Расход: {payload['spent_rub']:.4f} ₽",
        "",
        "| Блок | Заданий | Средний F1 |",
        "|---|---:|---:|",
    ]
    for target, values in sorted(by_target.items()):
        lines.append(f"| {target} | {len(values)} | {sum(values) / len(values):.4f} |")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"отчёт: {out / 'report.md'}")
    return 0


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
    if args.mode == "blocks":
        return await _run_blocks(args)
    cases_dir = Path(args.cases)
    if not cases_dir.exists():  # noqa: ASYNC240
        print(f"нет каталога дел: {cases_dir}")
        return 2
    cases = sorted(
        path
        for path in cases_dir.iterdir()  # noqa: ASYNC240
        if path.is_dir() and (path / "court_reference.txt").exists()
    )
    if args.require_amounts:
        amount_cases = []
        for path in cases:
            claim = path / "01_claim.txt"
            if claim.exists() and amounts_kop(claim.read_text(encoding="utf-8")):
                amount_cases.append(path)
        cases = amount_cases
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("нет дел с court_reference.txt")
        return 1

    settings = _settings(args.provider, args.model, args.max_tokens)
    template = load_template(settings)
    if args.provider == "openai" and os.environ.get(
        "ALLOW_CLOUD_LLM", ""
    ).lower() not in {"1", "true", "yes"}:
        print("Облачный провайдер отключён. Для включения: ALLOW_CLOUD_LLM=true.")
        return 2
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
    parser.add_argument("--tasks", default=str(DEFAULT_CASES.parent / "tasks.json"))
    parser.add_argument("--branches", default="")
    parser.add_argument("--targets", default="")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--require-amounts",
        action="store_true",
        help="только дела, где в иске есть рублёвая сумма",
    )
    parser.add_argument("--max-tokens", type=int, default=0)
    parser.add_argument("--budget-rub", type=float, default=2.0)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
