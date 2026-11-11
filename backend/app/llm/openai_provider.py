"""OpenAI-совместимый провайдер (ZvenoAI, DeepSeek Flash).

Один POST на /chat/completions. Модель выбирается на каждый запрос:
самая дешёвая из семейства DeepSeek Flash по ожидаемому размеру входа
и выхода (см. model_router.py). Ключ не логируется.

Ловушка DeepSeek, найденная замером в rector-dashboard: у модели есть
reasoning. При малом max_tokens она тратит весь бюджет на размышления
и возвращает ПУСТОЙ content с finish_reason=length. Поэтому:
  * reasoning_effort="none" для обычных задач;
  * max_tokens не меньше openai_max_tokens (700);
  * если пустой ответ всё же пришёл из-за length — один повтор с
    удвоенным бюджетом, и только потом ошибка.

Ошибки:
  * 408/425/429/500/502/503/504 — временные, повторяем с backoff;
  * 400/401/402/403/404/422 — не повторяем: ключ, деньги, кривой запрос.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from app.llm.base import LLMError, LLMRequest, LLMResponse, LLMUsage
from app.llm.model_router import ModelPrice, ModelRouter

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
EMPTY_RETRY_CAP = 2000


def _clean_reasoning_marks(text: str) -> str:
    """Убрать служебные метки размышления из ответа."""
    if not text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return cleaned.replace("</think>", "").replace("<think>", "").strip()


class OpenAIProvider:
    """Провайдер ZvenoAI. Удовлетворяет протоколу LLMProvider."""

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        model_auto: bool = True,
        model_filter: str = "deepseek",
        expected_out_tokens: int = 300,
        timeout_sec: int = 120,
        max_tokens: int = 700,
        temperature: float = 0.1,
        retries: int = 2,
        retry_delay: float = 0.6,
        reasoning_effort: str = "none",
        price_in: float = 8.3899704,
        price_out: float = 27.0644205,
        price_cache: float = 2.706442,
        price_ttl_sec: int = 21600,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._model = model
        self._model_auto = model_auto
        self._model_filter = model_filter.strip().lower() or "deepseek"
        self._expected_out_tokens = max(1, int(expected_out_tokens))
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._max_tokens = max(1, int(max_tokens))
        self._temperature = temperature
        self._retries = max(0, int(retries))
        self._retry_delay = max(0.0, float(retry_delay))
        self._reasoning_effort = reasoning_effort.strip()
        self._transport = transport
        self._fallback = ModelPrice(
            id=model,
            name=model,
            price_in=price_in,
            price_out=price_out,
            price_cache=price_cache,
        )
        self._router = ModelRouter(
            base_url=self._base_url,
            api_key=self._api_key,
            model_filter=self._model_filter,
            ttl_sec=price_ttl_sec,
            timeout_sec=30.0,
        )

    # ── Выбор модели ────────────────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(messages: list[Any]) -> int:
        """Кириллица ≈ 2,5 символа на токен; минимум 1."""
        chars = sum(len(str(getattr(message, "content", ""))) for message in messages)
        return max(1, int(chars / 2.5) + 1)

    def _expected_out(self, request: LLMRequest) -> int:
        requested = request.max_tokens or self._max_tokens
        return min(max(int(requested), 80), self._expected_out_tokens)

    async def _choose_model(self, request: LLMRequest) -> ModelPrice:
        if not self._model_auto:
            return self._fallback
        tokens_in = self._estimate_tokens(list(request.messages))
        tokens_out = self._expected_out(request)
        choice = await asyncio.to_thread(
            self._router.pick, tokens_in, tokens_out
        )
        if choice is None:
            logger.warning(
                "Цены моделей недоступны (%s), использую %s",
                self._router.last_error or "нет данных",
                self._fallback.id,
            )
            return self._fallback
        logger.debug(
            "Выбрана модель %s (вход=%s, выход=%s)",
            choice.id,
            tokens_in,
            tokens_out,
        )
        return choice

    # ── HTTP ────────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout_sec,
            transport=self._transport,
        )

    def _payload(
        self,
        request: LLMRequest,
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ]
        if not messages:
            messages = [{"role": "user", "content": " "}]
        if request.json_schema is not None:
            schema = json.dumps(request.json_schema, ensure_ascii=False)
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": (
                        "Верни только валидный JSON, соответствующий схеме: "
                        + schema
                    ),
                },
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": (
                request.temperature
                if request.temperature is not None
                else self._temperature
            ),
            "max_tokens": max_tokens,
        }
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.json_mode or request.json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        effort = self._reasoning_effort
        if request.reasoning is False:
            effort = "none"
        if effort:
            payload["reasoning_effort"] = effort
        return payload

    async def _one_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._client() as client:
                    response = await client.post(url, json=payload, headers=headers)
                if (
                    response.status_code in RETRYABLE_STATUS
                    and attempt < self._retries
                ):
                    last_error = LLMError(f"HTTP {response.status_code}")
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                    continue
                response.raise_for_status()
                parsed = response.json()
                if not isinstance(parsed, dict):
                    raise LLMError("ответ API не является JSON-объектом")
                return parsed
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if (
                    exc.response.status_code not in RETRYABLE_STATUS
                    or attempt >= self._retries
                ):
                    detail = exc.response.text[:300]
                    raise LLMError(
                        f"OpenAI API вернул {exc.response.status_code}: {detail}"
                    ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt >= self._retries:
                    raise LLMError(f"OpenAI API недоступен: {exc}") from exc
            except ValueError as exc:
                raise LLMError(f"ответ API не JSON: {exc}") from exc
            await asyncio.sleep(self._retry_delay * (attempt + 1))
        raise LLMError(f"OpenAI API: исчерпаны повторы: {last_error}")

    @staticmethod
    def _content_of(data: dict[str, Any]) -> tuple[str, str, str]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("в ответе API нет choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMError("в ответе API некорректный choices[0]")
        message = first.get("message")
        if not isinstance(message, dict):
            message = {}
        text = str(message.get("content") or "").strip()
        reasoning = str(
            message.get("reasoning") or message.get("reasoning_content") or ""
        )
        finish = str(first.get("finish_reason") or "")
        return _clean_reasoning_marks(text), finish, reasoning

    @staticmethod
    def _usage(data: dict[str, Any]) -> LLMUsage:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return LLMUsage(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    # ── Публичный интерфейс ─────────────────────────────────────────────

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY не задан")
        started = time.perf_counter()
        choice = await self._choose_model(request)
        max_tokens = max(
            int(request.max_tokens or self._max_tokens),
            self._max_tokens,
        )
        payload = self._payload(request, choice.id, max_tokens)
        data = await self._one_call(payload)
        text, finish, reasoning = self._content_of(data)

        if not text and finish == "length" and max_tokens < EMPTY_RETRY_CAP:
            retry_tokens = min(max_tokens * 2, EMPTY_RETRY_CAP)
            retry_payload = self._payload(request, choice.id, retry_tokens)
            retry_payload["reasoning_effort"] = "none"
            data = await self._one_call(retry_payload)
            text, finish, reasoning = self._content_of(data)

        if not text:
            hint = " (модель ушла в reasoning)" if reasoning else ""
            raise LLMError(
                f"Модель {choice.id} вернула пустой ответ{hint}, "
                f"finish={finish or 'нет'}"
            )

        return LLMResponse(
            text=text,
            usage=self._usage(data),
            model=choice.id,
            provider=self.name,
            is_mock=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=finish or "stop",
        )

    async def generate_json(
        self,
        request: LLMRequest,
        *,
        default: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        json_request = request.model_copy(update={"json_mode": True})
        try:
            response = await self.generate(json_request)
        except LLMError:
            return dict(default or {})

        parsed = self._parse_json(response.text)
        if parsed is not None:
            return parsed

        fenced = re.search(
            r"```(?:json)?\s*(.+?)```", response.text, re.DOTALL
        )
        if fenced:
            parsed = self._parse_json(fenced.group(1))
            if parsed is not None:
                return parsed
        return dict(default or {})

    async def health(self) -> bool:
        if not self._api_key:
            return False
        url = f"{self._base_url}/models"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(
                timeout=5, transport=self._transport
            ) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        models = payload.get("data")
        if not isinstance(models, list) or not models:
            return False
        if not self._model_auto:
            return any(
                isinstance(item, dict)
                and str(item.get("id") or "") == self._model
                for item in models
            )
        return any(
            isinstance(item, dict)
            and "flash" in str(item.get("id") or "").lower()
            and self._model_filter in str(item.get("id") or "").lower()
            for item in models
        )

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        candidate = text.strip()
        block = re.search(r"\{.*\}", candidate, re.DOTALL)
        if block:
            candidate = block.group(0)
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
