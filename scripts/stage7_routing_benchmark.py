"""Замер кэша ответов модели (Этап 7).

Что измеряется и чем это честно:

- накладные расходы кэша и время попадания — измеряются на провайдере с
  фиксированной задержкой 20 мс, чтобы латентность была видна, а не
  растворялась в шуме;
- идентичность ответа: попадание обязано вернуть тот же текст, а не
  «похожий» — иначе «качество не ниже» превращается в обещание;
- маршруты задач: печатается, куда какая задача маршрутизируется.

Что НЕ измеряется: задержка реальной модели. На этой машине REAL-прогон
не выполнялся (8 ГБ RAM, CPU-only), поэтому цифры для Vikhr появятся
только на RTX 3050 и в этот baseline не входят. Путать стенд с моделью
нельзя: 20 мс — это задержка стенда.

Запуск:

    backend\\.venv\\Scripts\\python.exe scripts\\stage7_routing_benchmark.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings  # noqa: E402
from app.llm.base import LLMRequest, LLMResponse, LLMRole  # noqa: E402
from app.model_routing.cache import ResponseCache  # noqa: E402
from app.model_routing.registry import TaskKind, routes_snapshot  # noqa: E402

STUB_DELAY_SECONDS = 0.02
PASSES = 2
PROMPTS = [
    "Вводная часть: стороны и предмет",
    "Описательная часть: доводы истца",
    "Мотивировочная часть: оценка доказательств",
    "Резолютивная часть: взыскать задолженность",
]


class SlowStubProvider:
    """Провайдер с предсказуемой задержкой — стенд для замера кэша."""

    name = "stage7-stub"
    _model = "stub-20ms"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(STUB_DELAY_SECONDS)
        self.calls += 1
        return LLMResponse(
            text=f"ответ на: {request.messages[-1].content}",
            model=self._model,
            provider=self.name,
            is_mock=True,
            latency_ms=int(STUB_DELAY_SECONDS * 1000),
        )


def request_for(prompt: str) -> LLMRequest:
    return LLMRequest(
        messages=[{"role": "user", "content": prompt}],
        role=LLMRole.GENERATION,
        temperature=0.1,
        max_tokens=420,
    )


async def measure() -> dict[str, object]:
    provider = SlowStubProvider()
    cache = ResponseCache()
    first_pass: list[int] = []
    second_pass: list[int] = []
    first_texts: dict[str, str] = {}
    mismatches: list[str] = []

    async def one_call(prompt: str) -> tuple[int, str]:
        started = time.perf_counter()
        response = await cache.generate(
            provider, request_for(prompt), task=TaskKind.SECTION_DRAFT
        )
        return int((time.perf_counter() - started) * 1000), response.text

    for prompt in PROMPTS:
        elapsed, text = await one_call(prompt)
        first_pass.append(elapsed)
        first_texts[prompt] = text

    for prompt in PROMPTS:
        elapsed, text = await one_call(prompt)
        second_pass.append(elapsed)
        if text != first_texts[prompt]:
            mismatches.append(prompt)

    return {
        "stub_delay_ms": int(STUB_DELAY_SECONDS * 1000),
        "unique_prompts": len(PROMPTS),
        "provider_calls": provider.calls,
        "first_pass_ms": first_pass,
        "second_pass_ms": second_pass,
        "cache": cache.stats(),
        "identical_outputs": not mismatches,
        "mismatched_prompts": mismatches,
        "note": (
            "Задержка стенда 20 мс, не реальной модели: REAL-прогон на этой "
            "машине не выполнялся."
        ),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = Settings(_env_file=None)
    result = asyncio.run(measure())
    result["routes"] = routes_snapshot(settings)
    result["cache_enabled_by_default"] = settings.response_cache_enabled
    result["ttl_seconds"] = settings.response_cache_ttl_seconds
    result["max_entries"] = settings.response_cache_max_entries

    print(json.dumps(result, ensure_ascii=False, indent=2))

    failures: list[str] = []
    # Ожидание: провайдер вызван ровно один раз на каждый уникальный запрос.
    if result["provider_calls"] != len(PROMPTS):
        failures.append(
            f'провайдер вызван {result["provider_calls"]} раз вместо {len(PROMPTS)}'
        )
    if not result["identical_outputs"]:
        failures.append("кэш вернул другой текст — качество изменилось")
    if result["cache"]["hit_ratio_percent"] != 50:
        failures.append(
            f'доля попаданий {result["cache"]["hit_ratio_percent"]}%, ожидалось 50'
        )

    out = Path(__file__).resolve().parents[1] / "docs" / "stage7-baseline.json"
    out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Baseline: {out}")

    if failures:
        print("НЕ ПРОШЛО: " + "; ".join(failures))
        return 1
    print(
        "ПРОШЛО: попадание отдаёт тот же текст, провайдер не вызывается повторно, "
        "доля попаданий 50%"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
