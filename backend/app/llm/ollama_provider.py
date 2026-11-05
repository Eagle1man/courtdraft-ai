"""Реальный LLM-провайдер через Ollama (режим RUNTIME, п.3, п.52).

Работает только при LLM_PROVIDER=ollama. На ноутбуке разработки используется
MockLLMProvider; переключение — только через .env, без правок кода (п.61).

Модель берётся из OLLAMA_MODEL и в коде не зашита (п.3).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from app.llm.base import LLMError, LLMRequest, LLMResponse, LLMUsage


def _clean_reasoning_marks(text: str) -> str:
    """Убрать служебные метки размышления из ответа.

    Даже при выключенном режиме размышления шаблон чата оставляет
    закрывающий тег в начале ответа: ответ начинался со строки
    «</think>». В проект решения такая строка попадала как текст.

    Удаляются только метки и блоки размышления; сам текст не
    трогается.
    """
    if not text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = cleaned.replace("</think>", "").replace("<think>", "")
    return cleaned.strip()


class OllamaProvider:
    """Провайдер поверх HTTP-интерфейса Ollama.

    Не импортирует пакет ollama: используется httpx. Причина — на DEV-машине
    пакет может быть не установлен, а импорт на уровне модуля уронил бы
    весь backend. Импорта нет вообще — это делает зависимость честной (п.55).
    """

    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_sec: int = 120,
        num_ctx: int = 4096,
        temperature: float = 0.1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_sec
        self._num_ctx = num_ctx
        self._temperature = temperature

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Вызвать Ollama /api/chat."""
        started = time.perf_counter()

        options: dict[str, Any] = {
            "num_ctx": self._num_ctx,
            "temperature": (
                request.temperature if request.temperature is not None else self._temperature
            ),
        }
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if request.stop:
            options["stop"] = list(request.stop)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "options": options,
            # РАЗМЫШЛЕНИЕ ВЫКЛЮЧАЕТСЯ ПО УМОЛЧАНИЮ.
            #
            # У моделей семейства Qwen3 в Ollama включён режим размышления
            # по умолчанию, и весь ответ уходит в отдельное поле thinking.
            # Поле content при этом пустое: система получала пустой текст
            # и считала, что модель не ответила.
            #
            # На этой машине размышление ещё и дорого: 1.7B на процессоре
            # i3 тратит на рассуждение минуты, а в проект решения оно всё
            # равно не попадает — следы рассуждений в тексте акта недопустимы.
            "think": bool(request.reasoning) if request.reasoning is not None else False,
        }
        # json_mode на уровне протокола Ollama не гарантирован, поэтому
        # требование JSON дублируется инструкцией в системном сообщении.
        # Если передана схема — Ollama ограничивает генерацию ею (Structured
        # Output), и модель физически не может вернуть поля другой формы.
        if request.json_schema is not None:
            payload["format"] = request.json_schema
        elif request.json_mode:
            payload["format"] = "json"

        url = f"{self._base_url}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise LLMError(f"Ollama не ответила за {self._timeout} c: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300]
            raise LLMError(f"Ollama вернула {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Не удалось достучаться до Ollama ({url}): {exc}") from exc

        text = (data.get("message") or {}).get("content", "")
        text = _clean_reasoning_marks(text)

        return LLMResponse(
            text=text,
            usage=LLMUsage(
                prompt_tokens=int(data.get("prompt_eval_count", 0)),
                completion_tokens=int(data.get("eval_count", 0)),
            ),
            model=str(data.get("model", self._model)),
            provider=self.name,
            is_mock=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=str(data.get("done_reason", "stop")),
        )

    async def generate_json(
        self,
        request: LLMRequest,
        *,
        default: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Получить JSON. При сбое — default, а не исключение.

        Агентам нужна устойчивость: отсутствие структурированного ответа
        должно приводить к пометке «требует проверки», а не к падению пайплайна.
        """
        json_request = request.model_copy(update={"json_mode": True})
        try:
            response = await self.generate(json_request)
        except LLMError:
            return dict(default or {})

        parsed = self._parse_json(response.text)
        if parsed is not None:
            return parsed

        # Модель может вернуть валидный JSON внутри обёртки ```json ... ```.
        fenced = re.search(r"```(?:json)?\s*(.+?)```", response.text, re.DOTALL)
        if fenced:
            parsed = self._parse_json(fenced.group(1))
            if parsed is not None:
                return parsed

        return dict(default or {})

    async def health(self) -> bool:
        """Проверить, что Ollama отвечает и нужная модель загружена."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                names = {m.get("name", "") for m in response.json().get("models", [])}
        except httpx.HTTPError:
            return False

        if not names:
            return False

        # Тег может быть записан как "qwen3.5" или "qwen3.5:4b".
        base = self._model.split(":")[0]
        return any(name == self._model or name.split(":")[0] == base for name in names)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        """Разобрать JSON-объект, терпимо к лишнему тексту вокруг."""
        candidate = text.strip()
        block = re.search(r"\{.*\}", candidate, re.DOTALL)
        if block:
            candidate = block.group(0)
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
