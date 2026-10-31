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
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    if _onnx_file(model) is not None:
        try:
            import onnxruntime  # type: ignore[import-untyped]  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            return False, (
                "нет onnxruntime/transformers: установите их "
                "(см. docs/injection-filter-model.md)"
            )
        return True, ""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
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


def _onnx_file(model: str) -> Path | None:
    """ONNX-файл в локальном каталоге модели, если он есть."""
    base = Path(model)
    if not base.is_dir():
        return None
    for name in ("model.quant.onnx", "model.onnx"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


#: Загруженные ONNX-сессии: путь модели -> (сессия, токенизатор, индекс, входы).
_onnx_loaded: dict[str, tuple[Any, Any, int, list[str]]] = {}


def _load_onnx(model: str) -> tuple[Any, Any, int, list[str]]:
    if model in _onnx_loaded:
        return _onnx_loaded[model]

    import onnxruntime as ort
    from transformers import AutoTokenizer

    onnx_path = _onnx_file(model)
    if onnx_path is None:  # pragma: no cover - проверено выше
        raise GuardUnavailableError("в каталоге модели нет ONNX-файла")

    tokenizer = AutoTokenizer.from_pretrained(model)
    session = ort.InferenceSession(
        str(onnx_path), providers=["CPUExecutionProvider"]
    )
    config = json.loads((Path(model) / "config.json").read_text(encoding="utf-8"))
    id2label = {
        int(index): str(label).upper()
        for index, label in config.get("id2label", {}).items()
    }
    malicious_index = next(
        (index for index, label in id2label.items() if "MALICIOUS" in label),
        max(id2label) if id2label else 1,
    )
    names = [item.name for item in session.get_inputs()]
    loaded = (session, tokenizer, malicious_index, names)
    _onnx_loaded[model] = loaded
    return loaded


def _classify_onnx(chunks: list[str], model: str) -> list[float]:
    """ONNX-путь: не требует torch, памяти нужно меньше."""
    import numpy as np

    session, tokenizer, malicious_index, input_names = _load_onnx(model)
    scores: list[float] = []
    for chunk in chunks:
        encoded = tokenizer(chunk, truncation=True, max_length=512)
        ids = list(encoded["input_ids"])
        feed = {}
        for name in input_names:
            if name == "input_ids":
                feed[name] = np.array([ids], dtype=np.int64)
            elif name == "attention_mask":
                feed[name] = np.ones((1, len(ids)), dtype=np.int64)
            elif name == "token_type_ids":
                feed[name] = np.zeros((1, len(ids)), dtype=np.int64)
        logits = np.asarray(session.run(None, feed)[0][0])
        shifted = np.exp(logits - logits.max())
        probabilities = shifted / shifted.sum()
        scores.append(float(probabilities[malicious_index]))
    return scores


#: Загруженные модели: ключ — имя, значение — (токенизатор, модель, индекс
#: метки). Без кэша оценка набора из 50 фрагментов загружала бы модель 50 раз.
_loaded: dict[str, tuple[object, object, int]] = {}


def _load(model: str) -> tuple[object, object, int]:
    if model in _loaded:
        return _loaded[model]

    import torch  # noqa: F401 - нужен внутри загрузки
    from transformers import (
        AutoModelForSequenceClassification,
        AutoTokenizer,
    )

    tokenizer = AutoTokenizer.from_pretrained(model)
    classifier = AutoModelForSequenceClassification.from_pretrained(model)
    classifier.eval()
    labels = {
        index: str(label).upper()
        for index, label in classifier.config.id2label.items()
    }
    malicious_index = next(
        (index for index, label in labels.items() if "MALICIOUS" in label),
        max(labels) if labels else 1,
    )
    loaded = (tokenizer, classifier, malicious_index)
    _loaded[model] = loaded
    return loaded


def classify_chunks(chunks: list[str], model: str) -> list[float]:
    """Вероятности «malicious» по окнам. Синхронно: вызывать в потоке."""
    if _onnx_file(model) is not None:
        return _classify_onnx(chunks, model)

    import torch

    tokenizer, classifier, malicious_index = _load(model)

    scores: list[float] = []
    with torch.no_grad():
        for chunk in chunks:
            encoded = tokenizer(  # type: ignore[operator]
                chunk, return_tensors="pt", truncation=True, max_length=512
            )
            logits = classifier(**encoded).logits  # type: ignore[operator]
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


async def score_text(text: str, *, model: str = DEFAULT_MODEL) -> float:
    """Одна оценка «malicious»: нужна для выбора порога на наборе."""
    return (await guard_check(text, model=model)).score
