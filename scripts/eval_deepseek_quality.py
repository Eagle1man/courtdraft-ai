#!/usr/bin/env python3
"""Пилот качества DeepSeek Flash на реальных учебных делах.

Что делает:

1. читает ключ/настройки из `.env` (ключ не печатается и не пишется в отчёт);
2. получает живой прайс DeepSeek Flash через `/v1/models` и выбирает модель
   так же, как приложение (`ModelRouter`);
3. прогоняет полный проект по каждому делу из `data/lora/eval_cases`
   (материалы дела — модели, `court_reference.txt` — только для сверки);
4. считает расход по фактическим `usage` каждого HTTP-вызова и жёстко
   останавливается при достижении бюджета;
5. пишет машинный отчёт, Markdown-сводку и пакет для показа судьям.

Запуск (из корня репозитория):

    backend\\.venv\\Scripts\\python.exe scripts\\eval_deepseek_quality.py --limit 1
    backend\\.venv\\Scripts\\python.exe scripts\\eval_deepseek_quality.py --budget-rub 4.5

Ключевые ограничения:

* бюджет — только на API; ретраи внутри провайдера отключены (`--retries 0`),
  чтобы расход был предсказуем;
* `court_reference.txt` в API не уходит;
* сырые тексты дел и проектов пишутся в `artifacts/`, а не в `docs/`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_harness as eh
from app.agents.draft_agent import DraftResult, generate_draft
from app.agents.evidence_pack import build_evidence_pack, load_template
from app.config import EmbeddingProviderName, LLMProviderName, Settings
from app.llm.base import LLMError
from app.llm.model_router import ModelPrice, ModelRouter
from app.llm.openai_provider import OpenAIProvider
from app.verification.engine import Severity, verify_draft
from eval_draft import (
    assess,
    load_case,
)

DEFAULT_CASES = ROOT / "data" / "lora" / "eval_cases"
DEFAULT_OUT = ROOT / "artifacts" / "deepseek-quality-2026-11-11"


class BudgetExceeded(LLMError):
    """Бюджет пилота исчерпан; следующий платный вызов не делается."""


class TrackingProvider(OpenAIProvider):
    """OpenAI-провайдер с учётом фактического расхода по каждому HTTP-вызову.

    Важно: считаем на уровне `_one_call`, а не `generate`, потому что
    провайдер может сделать повторный запрос при пустом ответе. Оба запроса
    оплачиваются, и оба должны попасть в счёт.
    """

    def __init__(
        self,
        *,
        budget_rub: float,
        prices: dict[str, ModelPrice],
        fallback_price: ModelPrice,
        ledger_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.budget_rub = max(0.01, float(budget_rub))
        self.spent_rub = 0.0
        self.calls: list[dict[str, Any]] = []
        self.current_case = ""
        self._prices = prices
        self._fallback_price = fallback_price
        self.ledger_path = Path(ledger_path) if ledger_path else None

    def _ledger(self, event: dict[str, Any]) -> None:
        if self.ledger_path is None:
            return
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            line = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            # Журнал не должен ломать сам прогон.
            pass

    def price_for(self, model: str) -> ModelPrice:
        return self._prices.get(model, self._fallback_price)

    def _estimate_request_cost(self, payload: dict[str, Any]) -> float:
        messages = payload.get("messages") or []
        chars = sum(len(str(item.get("content", ""))) for item in messages if isinstance(item, dict))
        # Кириллица ≈ 2 знака на токен в худшую сторону; для проверки
        # бюджета лучше переоценить, чем недосчитать.
        tokens_in = max(1, int(chars / 2.0) + 1)
        tokens_out = int(payload.get("max_tokens") or self._max_tokens)
        return self.price_for(str(payload.get("model") or "")).cost(tokens_in, tokens_out)

    async def _one_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.spent_rub >= self.budget_rub:
            raise BudgetExceeded(
                f"бюджет исчерпан: потрачено {self.spent_rub:.4f} ₽ из {self.budget_rub:.2f} ₽"
            )
        estimated = self._estimate_request_cost(payload)
        if self.spent_rub + estimated > self.budget_rub:
            raise BudgetExceeded(
                "следующий запрос может превысить бюджет: "
                f"потрачено {self.spent_rub:.4f} ₽, оценка запроса {estimated:.4f} ₽, "
                f"лимит {self.budget_rub:.2f} ₽"
            )

        started = time.perf_counter()
        try:
            data = await super()._one_call(payload)
        except Exception as exc:
            self._ledger(
                {
                    "status": "error",
                    "case": self.current_case,
                    "model": str(payload.get("model") or ""),
                    "estimated_rub": round(estimated, 6),
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )
            raise
        usage = self._usage(data)
        model = str(payload.get("model") or "")
        cost = self.price_for(model).cost(usage.prompt_tokens, usage.completion_tokens)
        self.spent_rub += cost
        call = {
            "case": self.current_case,
            "model": model,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "cost_rub": round(cost, 6),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
        self.calls.append(call)
        self._ledger({"status": "ok", "estimated_rub": round(estimated, 6), **call})
        if self.spent_rub > self.budget_rub:
            raise BudgetExceeded(
                f"бюджет превышен после оплаченного запроса: {self.spent_rub:.4f} ₽"
            )
        return data


@dataclass
class CaseQuality:
    case: str
    error: str = ""
    provider: str = ""
    model: str = ""
    elapsed_sec: float = 0.0
    cost_rub: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    sections: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    comparison: dict[str, Any] = field(default_factory=dict)
    p0: list[str] = field(default_factory=list)
    p1: list[str] = field(default_factory=list)
    outcome_system: list[str] = field(default_factory=list)
    outcome_court: list[str] = field(default_factory=list)
    draft_text: str = ""
    operative_text: str = ""
    reference_text: str = ""


def make_settings(args: argparse.Namespace) -> Settings:
    base = Settings()
    update: dict[str, Any] = {
        "llm_provider": LLMProviderName.OPENAI,
        "embedding_provider": EmbeddingProviderName.MOCK,
        "openai_model_auto": not bool(args.model),
        "openai_model_filter": "deepseek",
        "openai_retries": max(0, int(args.retries)),
        "response_cache_enabled": False,
    }
    if args.model:
        update["openai_model"] = args.model
    if args.max_tokens:
        update["openai_max_tokens"] = int(args.max_tokens)
    return base.model_copy(update=update)


def fetch_prices(settings: Settings) -> tuple[dict[str, ModelPrice], str]:
    """Живой прайс DeepSeek Flash; ключ не печатается."""
    router = ModelRouter(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model_filter=settings.openai_model_filter,
        ttl_sec=settings.openai_price_ttl_sec,
        timeout_sec=30.0,
    )
    items = router.candidates(force=True)
    prices = {item.id: item for item in items}
    return prices, router.last_error


def make_provider(
    settings: Settings,
    *,
    budget_rub: float,
    prices: dict[str, ModelPrice],
    ledger_path: str = str(ROOT / "artifacts" / "budget_ledger.jsonl"),
) -> TrackingProvider:
    fallback = ModelPrice(
        id=settings.openai_model,
        name=settings.openai_model,
        price_in=settings.openai_price_in,
        price_out=settings.openai_price_out,
        price_cache=settings.openai_price_cache,
    )
    return TrackingProvider(
        budget_rub=budget_rub,
        prices=prices,
        fallback_price=fallback,
        ledger_path=ledger_path,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        model_auto=settings.openai_model_auto,
        model_filter=settings.openai_model_filter,
        expected_out_tokens=settings.openai_expected_out_tokens,
        timeout_sec=settings.openai_timeout_sec,
        max_tokens=settings.openai_max_tokens,
        temperature=settings.openai_temperature,
        retries=settings.openai_retries,
        retry_delay=settings.openai_retry_delay,
        reasoning_effort=settings.openai_reasoning_effort,
        price_in=settings.openai_price_in,
        price_out=settings.openai_price_out,
        price_cache=settings.openai_price_cache,
        price_ttl_sec=settings.openai_price_ttl_sec,
    )


def operative_text_of(draft: DraftResult) -> str:
    """Резолютивная часть по ключу/имени, а не только по слову «резолютив»."""
    for section in draft.sections:
        name = (section.name or "").lower()
        if section.key == "operative" or "резолютив" in name or "решил" in name:
            return section.text or ""
    return ""


def p0_reasons(draft: DraftResult, report: Any, operative: str) -> list[str]:
    reasons: list[str] = []
    if draft.provider.lower() == "mock" or any(section.is_mock for section in draft.sections):
        reasons.append("провайдер вернул MOCK/заглушку")
    if not operative.strip():
        reasons.append("резолютивная часть пуста")
    if re.search(
        r"не указана сумма|не установлен размер|требования истца не установлены"
        r"|резолютивная часть не сформирована|указать\s*\(",
        operative,
        re.IGNORECASE,
    ):
        reasons.append("резолютивная часть содержит незаполненные места")
    for section in draft.sections:
        if not (section.text or "").strip():
            reasons.append(f"пустой раздел: {section.name}")
    seen: set[tuple[str, str]] = set()
    for issue in report.issues:
        if issue.severity is Severity.CRITICAL:
            key = (issue.type.value, issue.text[:120])
            if key in seen:
                continue
            seen.add(key)
            reasons.append(f"CRITICAL {issue.type.value}: {issue.text[:120]}")
    return reasons


def p1_reasons(report: Any) -> list[str]:
    reasons: list[str] = []
    seen: set[tuple[str, str]] = set()
    for issue in report.issues:
        if issue.severity in (Severity.HIGH, Severity.MEDIUM):
            key = (issue.type.value, issue.text[:120])
            if key in seen:
                continue
            seen.add(key)
            reasons.append(f"{issue.severity.value} {issue.type.value}: {issue.text[:140]}")
    return reasons


def _normalize_ru(text: str) -> str:
    return (text or "").replace("ё", "е").replace("Ё", "Е")


def _outcome_of(text: str) -> list[str]:
    """Маркеры исхода с нормализацией ё/е в обеих сторонах."""
    lowered = _normalize_ru(text).lower()
    found: list[str] = []
    for root, label in eh.OUTCOME_MARKERS:
        if _normalize_ru(root) in lowered and label not in found:
            found.append(label)
    return found


def compare_with_reference(draft: DraftResult, operative: str, reference: str) -> dict[str, Any]:
    system_outcome = _outcome_of(operative)
    court_outcome = _outcome_of(reference)
    system_laws = eh.laws_in(draft.text)
    court_laws = eh.laws_in(reference)
    laws = eh.laws_diff(system_laws, court_laws)
    court_amounts = eh.rub_amounts(reference)
    system_amounts = eh.rub_amounts(operative)
    return {
        "outcome_system": system_outcome,
        "outcome_court": court_outcome,
        "outcome_same": sorted(system_outcome) == sorted(court_outcome),
        "outcome_overlap": sorted(set(system_outcome) & set(court_outcome)),
        "laws": laws,
        "laws_both": len(laws["both"]),
        "laws_system_only": len(laws["system_only"]),
        "laws_court_only": len(laws["court_only"]),
        "court_amounts": [amount for amount, _ in court_amounts],
        "system_amounts": [amount for amount, _ in system_amounts],
        "court_amounts_found": sorted(
            {amount for amount, _ in court_amounts}
            & {amount for amount, _ in system_amounts}
        ),
    }


def section_rows(draft: DraftResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in draft.sections:
        rows.append(
            {
                "key": section.key,
                "name": section.name,
                "chars": len(section.text or ""),
                "built_by": section.built_by,
                "model": section.model,
                "prompt_tokens": section.prompt_tokens,
                "completion_tokens": section.completion_tokens,
                "needs_review": section.needs_review,
                "error": section.error,
            }
        )
    return rows


async def evaluate_case(
    case_dir: Path,
    *,
    settings: Settings,
    provider: TrackingProvider,
    template: dict[str, Any],
) -> CaseQuality:
    item = CaseQuality(case=case_dir.name)
    started = time.perf_counter()
    cost_before = provider.spent_rub
    provider.current_case = case_dir.name
    try:
        joined, documents, reference = load_case(case_dir)
        pack = build_evidence_pack(
            case_dir.name,
            "Учебное дело (реальные материалы)",
            "гражданское дело",
            documents,
            case_type="civil",
            settings=settings,
        )
        if settings.multi_agent_enabled:
            from app.agents.pipeline import run_pipeline

            pipeline = await run_pipeline(
                pack,
                template,
                provider=provider,
                settings=settings,
            )
            draft = pipeline.draft
            verify_report = pipeline.verification
        else:
            draft = await generate_draft(
                pack,
                template,
                settings=settings,
                provider=provider,
            )
            verify_report = verify_draft(draft, pack)
        summary = assess(draft, pack, case_dir.name, joined, reference)
        operative = operative_text_of(draft)
        comparison = compare_with_reference(draft, operative, reference)
        item.provider = draft.provider
        item.model = draft.model
        item.sections = section_rows(draft)
        item.verification = {
            "status": verify_report.status.value,
            "claims_checked": int(summary.claims_checked),
            "unsupported_claims": int(summary.unsupported_claims),
            "severe_issues": int(summary.severe_issues),
            "issues": [issue.to_dict() for issue in verify_report.issues],
        }
        item.comparison = comparison
        item.outcome_system = comparison["outcome_system"]
        item.outcome_court = comparison["outcome_court"]
        item.p0 = p0_reasons(draft, verify_report, operative)
        item.p1 = p1_reasons(verify_report)
        item.draft_text = draft.text
        item.operative_text = operative
        item.reference_text = reference
        item.prompt_tokens = sum(section.prompt_tokens for section in draft.sections)
        item.completion_tokens = sum(section.completion_tokens for section in draft.sections)
        item.elapsed_sec = round(time.perf_counter() - started, 1)
    except BudgetExceeded as exc:
        item.error = f"BUDGET: {exc}"
    except Exception as exc:  # noqa: BLE001 - отчёт обязан назвать причину
        item.error = f"{type(exc).__name__}: {exc}"
    item.cost_rub = round(provider.spent_rub - cost_before, 6)
    if item.elapsed_sec == 0.0:
        item.elapsed_sec = round(time.perf_counter() - started, 1)
    return item


def write_case_artifact(out_dir: Path, item: CaseQuality) -> None:
    target = out_dir / "cases" / f"{item.case}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(asdict(item), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def format_rub(value: float) -> str:
    return f"{value:.4f}"


def write_report(
    out_dir: Path,
    items: list[CaseQuality],
    *,
    model_rows: list[ModelPrice],
    settings: Settings,
    provider: TrackingProvider,
    budget_rub: float,
    started_at: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = [item for item in items if not item.error]
    total_cost = provider.spent_rub
    total_prompt = sum(item.prompt_tokens for item in ok)
    total_completion = sum(item.completion_tokens for item in ok)
    total_p0 = sum(1 for item in ok if item.p0)
    total_severe = sum(
        int(item.verification.get("severe_issues", 0)) for item in ok
    )
    total_issues = sum(len(item.verification.get("issues", [])) for item in ok)

    payload = {
        "date": started_at,
        "cases_dir": str(DEFAULT_CASES),
        "model_filter": settings.openai_model_filter,
        "model_auto": settings.openai_model_auto,
        "max_tokens": settings.openai_max_tokens,
        "temperature": settings.openai_temperature,
        "retries": settings.openai_retries,
        "budget_rub": budget_rub,
        "spent_rub": round(total_cost, 6),
        "cases_total": len(items),
        "cases_ok": len(ok),
        "cases_error": len(items) - len(ok),
        "p0_cases": total_p0,
        "severe_issues": total_severe,
        "issues": total_issues,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "prices": [asdict(row) for row in model_rows],
        "calls": provider.calls,
        "cases": [asdict(item) for item in items],
    }
    (out_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines: list[str] = []
    lines.append("# Проверка качества DeepSeek Flash на учебных делах")
    lines.append("")
    lines.append(f"- Дата: {started_at}")
    lines.append(f"- Дел: {len(items)} (успешно {len(ok)}, с ошибкой {len(items) - len(ok)})")
    lines.append(f"- Израсходовано: **{format_rub(total_cost)} ₽** из {budget_rub:.2f} ₽")
    lines.append(f"- Токены: вход {total_prompt}, выход {total_completion}")
    lines.append(f"- Дел с P0-блокерами: **{total_p0}**")
    lines.append(f"- Замечаний: всего {total_issues}, тяжёлых {total_severe}")
    lines.append("")
    lines.append("## Модели и цены (живой прайс `/v1/models`)")
    lines.append("")
    lines.append("| Модель | Вход, ₽/1M | Выход, ₽/1M |")
    lines.append("|---|---:|---:|")
    for row in model_rows:
        lines.append(f"| `{row.id}` | {row.price_in:.4f} | {row.price_out:.4f} |")
    if not model_rows:
        lines.append("| — | — | — |")
    lines.append("")
    lines.append("## Сводка")
    lines.append("")
    lines.append("| Дело | Исход ИИ / суд | Суммы суда найдены | Нормы (общие/только ИИ/только суд) | Разделов | CRITICAL/HIGH | P0 | Токены | ₽ | сек |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for item in items:
        if item.error:
            lines.append(f"| {item.case} | — | — | — | — | — | ошибка | — | {format_rub(item.cost_rub)} | {item.elapsed_sec} |")
            continue
        ver = item.verification
        issues = ver.get("issues", [])
        critical = sum(1 for issue in issues if issue.get("severity") == "CRITICAL")
        high = sum(1 for issue in issues if issue.get("severity") == "HIGH")
        comp = item.comparison
        outcome = " / ".join(
            [
                ", ".join(item.outcome_system) or "—",
                ", ".join(item.outcome_court) or "—",
            ]
        )
        lines.append(
            f"| {item.case} | {outcome} | "
            f"{len(comp.get('court_amounts_found', []))}/{len(comp.get('court_amounts', []))} | "
            f"{comp.get('laws_both', 0)}/{comp.get('laws_system_only', 0)}/{comp.get('laws_court_only', 0)} | "
            f"{len(item.sections)} | {critical}/{high} | "
            f"{len(item.p0)} | {item.prompt_tokens + item.completion_tokens} | "
            f"{format_rub(item.cost_rub)} | {item.elapsed_sec} |"
        )
    lines.append("")
    lines.append("## Блокеры и ошибки")
    lines.append("")
    any_problem = False
    for item in items:
        if item.error:
            any_problem = True
            lines.append(f"- **{item.case}**: ошибка — {item.error}")
        if item.p0:
            any_problem = True
            lines.append(f"- **{item.case}**: P0 — " + "; ".join(item.p0))
    if not any_problem:
        lines.append("- P0-блокеров и ошибок нет.")
    lines.append("")
    lines.append("## Замечания проверки (не P0)")
    lines.append("")
    any_p1 = False
    for item in items:
        if item.p1:
            any_p1 = True
            lines.append(f"### {item.case}")
            for reason in item.p1[:12]:
                lines.append(f"- {reason}")
            if len(item.p1) > 12:
                lines.append(f"- … ещё {len(item.p1) - 12}")
            lines.append("")
    if not any_p1:
        lines.append("- Замечаний HIGH/MEDIUM нет.")
    lines.append("")
    lines.append("## Ограничения")
    lines.append("")
    lines.append("- Это пилот на учебных делах, а не статистически значимая выборка.")
    lines.append("- `court_reference.txt` — одно решение суда; расхождение не всегда ошибка ИИ.")
    lines.append("- Модель пишет только разделы `УСТАНОВИЛ` и мотивировочную; остальное собирают правила.")
    lines.append("- Автопроверка ловит выдуманные факты/нормы, но не заменяет юридическую оценку.")
    lines.append("- Сырые тексты лежат в `artifacts/`, в git не коммитятся.")
    lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_judge_pack(out_dir: Path, items: list[CaseQuality], *, limit: int = 3) -> None:
    candidates = [item for item in items if not item.error and not item.p0]
    candidates.sort(
        key=lambda item: (
            int(item.verification.get("severe_issues", 0)),
            -len(item.sections),
            item.cost_rub,
        )
    )
    chosen = candidates[:limit]
    lines: list[str] = []
    lines.append("# Пакет для показа судьям (черновик)")
    lines.append("")
    lines.append("Это не заключение и не замена судье. Ниже — проект, который система")
    lines.append("сгенерировала по учебным материалам, и решение суда по тому же делу.")
    lines.append("Оценка человека обязательна.")
    lines.append("")
    for item in chosen:
        lines.append(f"## {item.case}")
        lines.append("")
        lines.append("### Исход")
        lines.append(f"- ИИ: {', '.join(item.outcome_system) or '—'}")
        lines.append(f"- суд: {', '.join(item.outcome_court) or '—'}")
        lines.append("")
        lines.append("### Проверка")
        ver = item.verification
        lines.append(f"- замечаний: {len(ver.get('issues', []))}; тяжёлых: {ver.get('severe_issues', 0)}")
        for issue in ver.get("issues", [])[:10]:
            lines.append(f"- [{issue.get('severity')}] {issue.get('type')}: {issue.get('text', '')[:160]}")
        lines.append("")
        lines.append("### Проект ИИ")
        lines.append("")
        lines.append("```text")
        lines.append(item.draft_text[:12000])
        lines.append("```")
        lines.append("")
        lines.append("### Решение суда (эталон)")
        lines.append("")
        lines.append("```text")
        lines.append(item.reference_text[:8000])
        lines.append("```")
        lines.append("")
    (out_dir / "judge-demo-pack.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    settings = make_settings(args)
    if not settings.openai_api_key:
        print("НЕТ КЛЮЧА: OPENAI_API_KEY не задан в .env")
        return 2

    prices, price_error = fetch_prices(settings)
    model_rows = sorted(prices.values(), key=lambda row: (row.price_in, row.price_out, row.id))
    print("Модели DeepSeek Flash (живой прайс):")
    for row in model_rows:
        print(f"  {row.id}: вход {row.price_in:.4f} ₽/1M, выход {row.price_out:.4f} ₽/1M")
    if price_error:
        print(f"  ВНИМАНИЕ: прайс не обновлён: {price_error}")
    if not model_rows:
        print("НЕТ ЦЕН: не удалось получить список моделей; использую цены из конфига")

    provider = make_provider(settings, budget_rub=args.budget_rub, prices=prices)
    healthy = await provider.health()
    for _ in range(3):
        if healthy:
            break
        await asyncio.sleep(4)
        healthy = await provider.health()
    print(f"Ключ/модели: {'OK' if healthy else 'НЕДОСТУПНЫ'}")
    if not healthy:
        print("Проверка не запущена: API/ключ недоступны.")
        return 2

    cases = sorted(
        path
        for path in DEFAULT_CASES.glob("case-*")
        if path.is_dir() and (path / "court_reference.txt").exists()
    )
    if args.case:
        wanted = set(args.case)
        cases = [path for path in cases if path.name in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("Нет дел с court_reference.txt")
        return 1

    out_dir = Path(args.out or DEFAULT_OUT)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = load_template(settings)
    started_at = time.strftime("%Y-%m-%d %H:%M")
    print(
        f"Дел: {len(cases)}; бюджет: {args.budget_rub:.2f} ₽; "
        f"модель: {settings.openai_model if not settings.openai_model_auto else 'auto (deepseek flash)'}"
    )
    print(f"Артефакты: {out_dir}")

    items: list[CaseQuality] = []
    for number, case_dir in enumerate(cases, start=1):
        print(f"[{number}/{len(cases)}] {case_dir.name} …", flush=True)
        item = await evaluate_case(
            case_dir,
            settings=settings,
            provider=provider,
            template=template,
        )
        items.append(item)
        write_case_artifact(out_dir, item)
        status = "ошибка" if item.error else f"P0={len(item.p0)}, ₽{item.cost_rub:.4f}, {item.elapsed_sec}s"
        print(f"    {status}; всего {provider.spent_rub:.4f} ₽")
        if item.error.startswith("BUDGET"):
            print("ОСТАНОВ: бюджет исчерпан.")
            break

    write_report(
        out_dir,
        items,
        model_rows=model_rows,
        settings=settings,
        provider=provider,
        budget_rub=args.budget_rub,
        started_at=started_at,
    )
    write_judge_pack(out_dir, items)
    print(f"Готово. Отчёт: {out_dir / 'report.md'}")
    print(f"Пакет для судей: {out_dir / 'judge-demo-pack.md'}")
    print(f"Израсходовано: {provider.spent_rub:.4f} ₽")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-rub", type=float, default=4.5, help="жёсткий лимит, ₽")
    parser.add_argument("--limit", type=int, default=0, help="0 — все дела")
    parser.add_argument("--case", nargs="*", default=None, help="конкретные case-001 …")
    parser.add_argument("--model", default="", help="зафиксировать модель вместо авто")
    parser.add_argument("--max-tokens", type=int, default=0, help="переопределить лимит ответа")
    parser.add_argument("--retries", type=int, default=0, help="ретраи провайдера (0 — без скрытого расхода)")
    parser.add_argument("--out", default="", help="каталог артефактов")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
