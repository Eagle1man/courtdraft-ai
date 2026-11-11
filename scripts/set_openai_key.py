r"""Записать ключ ZvenoAI в .env, не печатая его.

Запуск:
    python scripts/set_openai_key.py \
      --key-file "C:\Users\diorl\OneDrive\Рабочий стол\ИИ Судья ключ.txt"

Скрипт:
  * проверяет, что ключ похож на sk-...;
  * делает .env.bak;
  * заменяет LLM_PROVIDER и OPENAI_*;
  * печатает только «OK» и путь, без значения ключа.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ENV_KEYS = {
    "LLM_PROVIDER": "openai",
    "OPENAI_BASE_URL": "https://api.zveno.ai/v1",
    "OPENAI_MODEL": "deepseek/deepseek-v4-flash-0731",
    "OPENAI_MODEL_AUTO": "true",
    "OPENAI_MODEL_FILTER": "deepseek",
    "OPENAI_EXPECTED_OUT_TOKENS": "300",
    "OPENAI_TIMEOUT_SEC": "120",
    "OPENAI_MAX_TOKENS": "700",
    "OPENAI_TEMPERATURE": "0.1",
    "OPENAI_RETRIES": "2",
    "OPENAI_RETRY_DELAY": "0.6",
    "OPENAI_REASONING_EFFORT": "none",
    "OPENAI_PRICE_IN": "8.3899704",
    "OPENAI_PRICE_OUT": "27.0644205",
    "OPENAI_PRICE_CACHE": "2.706442",
    "OPENAI_PRICE_TTL_SEC": "21600",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    key_path = Path(args.key_file)
    try:
        key = key_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        print(f"ОШИБКА: не удалось прочитать файл ключа: {exc}")
        return 2
    if not key.startswith("sk-") or len(key) < 20:
        print("ОШИБКА: файл ключа не похож на ключ (ожидается sk-...).")
        return 2

    env_path = Path(args.env_file)
    if env_path.exists():
        shutil.copy2(env_path, env_path.with_suffix(env_path.suffix + ".bak"))
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    updates = dict(ENV_KEYS)
    updates["OPENAI_API_KEY"] = key
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        name = stripped.split("=", 1)[0].strip()
        if name in updates:
            out.append(f"{name}={updates[name]}")
            seen.add(name)
        else:
            out.append(line)

    if out and out[-1].strip():
        out.append("")
    out.append("# OpenAI-совместимый провайдер (ZvenoAI / DeepSeek Flash)")
    for name, value in updates.items():
        if name not in seen:
            out.append(f"{name}={value}")

    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"OK: {env_path} обновлён. Ключ не показан.")
    print(f"Резервная копия: {env_path}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
