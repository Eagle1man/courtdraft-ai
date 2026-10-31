"""Необязательный смысловой фильтр инструкций (prompt injection).

Детерминированный детектор (`injection.py`) ловит известные формулировки.
Он не поймает перефразированное указание, поэтому есть второй слой —
классификатор, обученный отличать «попытку переопределить инструкции» от
обычного текста.

Модель по умолчанию: `meta-llama/Llama-Prompt-Guard-2-86M`
(86M параметров, mDeBERTa, бинарная классификация benign/malicious,
контекст 512 токенов — текст режется на окна). Обоснование выбора и
альтернативы — `docs/injection-filter-model.md`.

Слой выключен по умолчанию: это отдельная зависимость (transformers +
torch) и отдельная модель, её наличие — решение владельца. Если фильтр
включён, но модели/библиотек нет, это **не** повод отклонять документ:
предупреждение говорит «фильтр недоступен», а документ сохраняется.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

DEFAULT_MODEL = "meta-llama/Llama-Prompt-Guard-2-86M"
#: 512 токенов промпта — примерно 1500 символов русского текста.
CHUNK_CHARS = 1400
CHUNK_OVERLAP = 200


class GuardUnavailableError(RuntimeError):
    """Фильтр включён, но модель или библиотеки недоступны."""


@dataclass(frozen=True)
class GuardVerdict:
    score: float
    model: str
    chunks: int

    @property
    def suspicious(self) -> bool:
        return self.score >= 0.5


def availability(model: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """(можно ли использовать фильтр, причина отказа)."""
    try:
        import torch  # type: ignore[import-not-found]  # noqa: F401
        import transformers  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False, (
            "нет transformers/torch: установите их и скачайте модель "
            "(см. docs/injection-filter-model.md)"
        )
    return True, ""


def _split(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def classify_chunks(chunks: list[str], model: str) -> list[float]:
    """Вероятности «malicious» по окнам. Синхронно: вызывать в потоке."""
    import torch
    from transformers import (
        AutoModelForSequenceClassification,
        AutoTokenizer,
    )

    tokenizer = AutoTokenizer.from_pretrained(model)
    classifier = AutoModelForSequenceClassification.from_pretrained(model)
    classifier.eval()

    labels = {index: str(label).upper() for index, label in classifier.config.id2label.items()}
    malicious_index = next(
        (index for index, label in labels.items() if "MALICIOUS" in label),
        max(labels) if labels else 1,
    )

    scores: list[float] = []
    with torch.no_grad():
        for chunk in chunks:
            encoded = tokenizer(
                chunk, return_tensors="pt", truncation=True, max_length=512
            )
            logits = classifier(**encoded).logits
            probabilities = torch.softmax(logits, dim=-1)[0]
            scores.append(float(probabilities[malicious_index]))
    return scores


async def guard_check(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
) -> GuardVerdict:
    """Проверить текст. Бросает GuardUnavailableError с причиной."""
    ok, reason = availability(model)
    if not ok:
        raise GuardUnavailableError(reason)
    chunks = _split(text)
    if not chunks:  # pragma: no cover - пустой текст отсекается раньше
        raise GuardUnavailableError("пустой текст нечего проверять")

    try:
        scores = await asyncio.to_thread(classify_chunks, chunks, model)
    except Exception as exc:  # noqa: BLE001 - модель может не скачаться
        raise GuardUnavailableError(f"фильтр не отработал: {exc}") from exc

    worst = max(scores) if scores else 0.0
    return GuardVerdict(score=worst, model=model, chunks=len(chunks))
