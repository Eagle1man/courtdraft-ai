"""Замер скорости Ollama при разном числе потоков.

Один и тот же промпт, одинаковая длина ответа. Модель остаётся загруженной,
поэтому замеряются именно prefill и генерация, а не загрузка с диска.
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:11434"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "wavecut/vikhr:7b-instruct_0.4-Q4_1"

PROMPT = (
    "Ты помощник мирового судьи. По материалам дела составь проект "
    "описательной части судебного решения. Истец Смирнова Ольга Викторовна "
    "обратилась с иском к Кузнецову Андрею Петровичу о взыскании долга по "
    "договору займа в размере 150 000 рублей, процентов за пользование "
    "чужими денежными средствами в размере 12 340 рублей и расходов по "
    "уплате государственной пошлины в размере 4 200 рублей. Ответчик иск "
    "не признал, ссылаясь на возврат части суммы. В материалах дела есть "
    "договор займа, расписка, возражение ответчика и протокол заседания. "
    "Напиши связный текст описательной части на русском языке, без "
    "выдуманных фактов, ссылаясь только на перечисленные обстоятельства."
)


def run(client: httpx.Client, threads: int, ctx: int, predict: int) -> dict[str, object]:
    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "stream": False,
        "options": {
            "num_thread": threads,
            "num_ctx": ctx,
            "num_predict": predict,
            "temperature": 0,
            "seed": 42,
        },
    }
    started = time.perf_counter()
    response = client.post("/api/generate", json=payload)
    response.raise_for_status()
    wall = time.perf_counter() - started
    data = response.json()
    eval_count = data.get("eval_count", 0) or 0
    eval_ns = data.get("eval_duration", 0) or 0
    prompt_count = data.get("prompt_eval_count", 0) or 0
    prompt_ns = data.get("prompt_eval_duration", 0) or 0
    return {
        "threads": threads,
        "ctx": ctx,
        "wall": wall,
        "prompt_count": prompt_count,
        "prompt_rate": prompt_count / (prompt_ns / 1e9) if prompt_ns else 0.0,
        "eval_count": eval_count,
        "eval_rate": eval_count / (eval_ns / 1e9) if eval_ns else 0.0,
        "load_s": (data.get("load_duration", 0) or 0) / 1e9,
    }


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=900.0) as client:
        print(f"model: {MODEL}")
        warm = run(client, threads=2, ctx=512, predict=1)
        print(f"warmup: load={warm['load_s']:.1f}s wall={warm['wall']:.1f}s")
        for threads in (2, 4, 6, 8):
            result = run(client, threads=threads, ctx=2048, predict=64)
            print(
                f"threads={result['threads']:>2} ctx={result['ctx']} "
                f"prefill={result['prompt_rate']:.2f} t/s "
                f"({result['prompt_count']} tok) "
                f"gen={result['eval_rate']:.2f} t/s "
                f"({result['eval_count']} tok) "
                f"wall={result['wall']:.1f}s"
            )


if __name__ == "__main__":
    main()
