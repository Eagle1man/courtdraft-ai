"""Единый интерфейс LLM.

По умолчанию — FakeLLM: новый модуль не делает платных вызовов без явного
включения. Реальный провайдер берётся из существующего механизма проекта
(`app.llm`) либо из OpenAI-совместимого endpoint через переменные окружения.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol


class LLMClient(Protocol):
    name: str

    async def generate_json(self, system: str, user: str) -> dict[str, Any]: ...


class LLMError(RuntimeError):
    pass


def _parse_json(text: str) -> dict[str, Any]:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    if not t.startswith("{"):
        b = re.search(r"\{.*\}", t, re.S)
        if not b:
            raise LLMError("в ответе нет JSON-объекта")
        t = b.group(1)
    data = json.loads(t)
    if not isinstance(data, dict):
        raise LLMError("JSON не объект")
    return data


class FakeLLM:
    """Тест/демо: отдаёт заранее заданный JSON. Ничего не выдумывает."""

    name = "fake"

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.answer = answer or {}

    async def generate_json(self, system: str, user: str) -> dict[str, Any]:
        data = json.loads(json.dumps(self.answer))
        return data if isinstance(data, dict) else {}


class OpenAICompat:
    """OpenAI-совместимый endpoint (DeepSeek/ZvenoAI/Ollama /v1)."""

    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        temperature: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    async def generate_json(self, system: str, user: str) -> dict[str, Any]:
        import httpx

        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.base_url + "/chat/completions", json=payload, headers=headers
            )
        if response.status_code != 200:
            raise LLMError(f"LLM HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return _parse_json(text)


class HostAdapter:
    """Обёртка над существующим `app.llm` хост-проекта."""

    name = "host"

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def generate_json(self, system: str, user: str) -> dict[str, Any]:
        from app.llm.base import LLMMessage, LLMRequest, LLMRole

        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=system),
                LLMMessage(role="user", content=user),
            ],
            role=LLMRole.GENERATION,
            temperature=0.0,
            max_tokens=1500,
            json_mode=True,
        )
        response = await self.provider.generate(request)
        return _parse_json(response.text)


def make_llm(mode: str | None = None) -> LLMClient:
    """Создать клиент.

    `mode` по умолчанию берётся из `PASSPORT_LLM_MODE`; если не задан —
    безопасный `mock`. Режимы `host`/`openai`/`auto` требуют явного включения.
    """
    selected = (
        mode if mode is not None else (os.getenv("PASSPORT_LLM_MODE") or "mock")
    ).strip().lower()
    if selected in {"mock", "fake"}:
        return FakeLLM()
    if selected in {"auto", "host"}:
        try:
            from app.config import get_settings
            from app.llm import build_llm_provider

            return HostAdapter(build_llm_provider(get_settings()))
        except Exception:
            if selected == "host":
                raise
    base = os.getenv("PASSPORT_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL", "")
    if base:
        return OpenAICompat(
            base,
            os.getenv("PASSPORT_LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            os.getenv("PASSPORT_LLM_MODEL", os.getenv("OPENAI_MODEL", "")),
        )
    return FakeLLM()
