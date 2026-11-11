#!/usr/bin/env python3
"""Выбор самой дешёвой модели DeepSeek Flash по размеру запроса.

Запуск:
    python scripts/pick_deepseek_model.py --tokens-in 200 --tokens-out 30
    python scripts/pick_deepseek_model.py --text data/demo/01_claim.txt --tokens-out 300
    python scripts/pick_deepseek_model.py --tokens-in 227 --tokens-out 20 --json

Ключ не обязателен: список моделей и цены ZvenoAI отдаёт без авторизации.
Если ключ задан, он используется, но никогда не печатается.

Формула:
    cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
Выбирается минимальный cost. Цены — рубли за 1M токенов.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.zveno.ai/v1"


@dataclass(frozen=True)
class ModelPrice:
    id: str
    name: str
    price_in: float
    price_out: float
    price_cache: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000


def read_key(args: argparse.Namespace) -> str:
    """Ключ из --key-file или переменных окружения. В вывод не попадает."""
    if args.key_file:
        return Path(args.key_file).read_text(encoding="utf-8").strip()
    for name in ("ZVENO_API_KEY", "OPENAI_API_KEY", "RECTOR_ZVNO_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def fetch_models(base_url: str, key: str, timeout: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/models"
    headers = {
        "Accept": "application/json",
        "User-Agent": "courtdraft-picker/1.0",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ответ /models не является JSON-объектом")
    return payload


def parse_models(payload: dict[str, Any], model_filter: str) -> list[ModelPrice]:
    result: list[ModelPrice] = []
    wanted = model_filter.strip().lower()
    for raw in payload.get("data") or []:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("id") or "").strip()
        low = model_id.lower()
        if not model_id or model_id.startswith("~"):
            continue
        if "flash" not in low or wanted not in low:
            continue
        if "vision" in low:
            # Экспериментальная мультимодальная модель: цена на порядок выше.
            continue
        pricing = raw.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        try:
            price_in = float(pricing.get("prompt") or 0) * 1_000_000
            price_out = float(pricing.get("completion") or 0) * 1_000_000
            price_cache = float(pricing.get("input_cache_read") or 0) * 1_000_000
        except (TypeError, ValueError):
            continue
        if price_in <= 0 or price_out <= 0:
            continue
        result.append(
            ModelPrice(
                id=model_id,
                name=str(raw.get("name") or model_id),
                price_in=price_in,
                price_out=price_out,
                price_cache=price_cache,
            )
        )
    return result


def estimate_tokens(text: str, chars_per_token: float) -> int:
    """Оценка токенов по символам: русский текст ≈ 2,5 символа на токен."""
    return max(1, math.ceil(len(text) / chars_per_token))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokens-in", type=int, default=0)
    parser.add_argument("--tokens-out", type=int, default=300)
    parser.add_argument(
        "--text",
        default="",
        help="файл с запросом или '-' для stdin; токены входа считаются по символам",
    )
    parser.add_argument("--chars-per-token", type=float, default=2.5)
    parser.add_argument("--key-file", default="")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--filter", default="deepseek")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="оставлено для совместимости: CLI всегда запрашивает свежие цены",
    )
    args = parser.parse_args(argv)

    if args.tokens_in <= 0:
        if not args.text:
            parser.error("нужен --tokens-in или --text")
        try:
            raw_text = (
                sys.stdin.read()
                if args.text == "-"
                else Path(args.text).read_text(encoding="utf-8")
            )
        except OSError as exc:
            print(f"ОШИБКА: не удалось прочитать текст: {exc}", file=sys.stderr)
            return 2
        args.tokens_in = estimate_tokens(raw_text, args.chars_per_token)
    if args.tokens_out <= 0:
        parser.error("--tokens-out должен быть > 0")

    key = read_key(args)
    try:
        payload = fetch_models(args.base_url, key, args.timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"ОШИБКА: не удалось получить список моделей: {exc}", file=sys.stderr)
        return 3

    models = parse_models(payload, args.filter)
    if not models:
        print(
            "ОШИБКА: среди моделей нет DeepSeek Flash с положительными ценами",
            file=sys.stderr,
        )
        return 4

    ranked = sorted(
        models,
        key=lambda model: (model.cost(args.tokens_in, args.tokens_out), model.id),
    )
    best = ranked[0]
    best_cost = best.cost(args.tokens_in, args.tokens_out)

    if args.json:
        print(
            json.dumps(
                {
                    "tokens_in": args.tokens_in,
                    "tokens_out": args.tokens_out,
                    "chars_per_token": args.chars_per_token,
                    "winner": {
                        "id": best.id,
                        "name": best.name,
                        "price_in": best.price_in,
                        "price_out": best.price_out,
                        "cost": best_cost,
                    },
                    "candidates": [
                        {
                            "id": model.id,
                            "name": model.name,
                            "price_in": model.price_in,
                            "price_out": model.price_out,
                            "cost": model.cost(args.tokens_in, args.tokens_out),
                        }
                        for model in ranked
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(
        f"Запрос: вход={args.tokens_in} токенов, "
        f"выход={args.tokens_out} токенов"
    )
    print(
        f"{'Модель':<42} {'вход ₽/1M':>11} {'выход ₽/1M':>11} "
        f"{'стоимость ₽':>13} {'+ к лучшей':>11}"
    )
    for model in ranked:
        cost = model.cost(args.tokens_in, args.tokens_out)
        delta = (cost - best_cost) / best_cost * 100
        print(
            f"{model.id:<42} {model.price_in:>11.4f} {model.price_out:>11.4f} "
            f"{cost:>13.6f} {delta:>10.1f}%"
        )
    print()
    print(f"ВЫБОР: {best.id} — {best_cost:.6f} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
