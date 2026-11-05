"""Извлечение фактов из материалов дела (этап 2 ТЗ, §6).

Модель получает ограниченный контекст и обязана вернуть JSON заданной
формы (Ollama Structured Output). Всё, что можно проверить
детерминированно — суммы, даты, наличие цитаты в тексте, — проверяет
Python, а не модель.

Ключевое правило: непонятное значение не подменяется правдоподобным.
Сумма «сто тысяч» не превращается в 100000, дата 31.02.2025 не
сдвигается на 28.02. Вместо этого появляется запись в `missing` с
причиной, и реестр получает статус `needs_review`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.extraction.schemas import (
    CaseAmount,
    CaseClaim,
    CaseDate,
    CaseFact,
    CaseParticipant,
    CaseRegistry,
    ExtractionPayload,
    ExtractionStatus,
    FactStatus,
    MissingInformation,
    SourceRef,
    normalize_role,
    parse_date,
    parse_money,
)
from app.llm.base import LLMMessage, LLMRequest, LLMRole

logger = logging.getLogger(__name__)

#: Сколько знаков материалов отдавать модели за один вызов.
MAX_CONTEXT_CHARS = 6000

#: Сколько раз пробовать. Больше — бессмысленно: если модель не заполнила
#: схему дважды, она не заполнит её и на пятый раз, а время будет съедено.
MAX_ATTEMPTS = 2

SYSTEM_PROMPT = """Ты — экстрактор фактов из материалов судебного дела.

Верни только JSON. Никаких пояснений, выводов и оценок.

Правила:
1. Не придумывай значения. Если данных нет — оставь поле пустым.
2. Суммы — как в документе, с разделителями (например "1 234,56").
3. Даты — как в документе (например "15.01.2025").
4. В поле quote — дословная фраза из текста, откуда взято значение.
5. Не смешивай требования истца и установленные обстоятельства.
6. Не объединяй разные события в одну запись.
"""


class ChunkLike(Protocol):
    """Минимум, который нужен от фрагмента документа."""

    text: str
    page: int | None
    paragraph: int | None


class JsonProvider(Protocol):
    """Провайдер, умеющий вернуть JSON-объект."""

    name: str

    async def generate_json(
        self, request: LLMRequest, *, default: dict | None = None
    ) -> dict: ...


@dataclass
class ExtractionRun:
    """Результат извлечения: реестр и то, как он получен."""

    registry: CaseRegistry
    status: ExtractionStatus
    attempts: int
    errors: list[str] = field(default_factory=list)


def _context(chunks: Sequence[ChunkLike], *, max_chars: int) -> str:
    """Собрать контекст из фрагментов с указанием места."""
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        text = (chunk.text or "").strip()
        if not text:
            continue
        label = []
        if chunk.page is not None:
            label.append(f"с. {chunk.page}")
        if chunk.paragraph is not None:
            label.append(f"абз. {chunk.paragraph}")
        header = f"[{' ,'.join(label)}]" if label else "[фрагмент]"
        block = f"{header}\n{text}"
        if parts and used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def _locate(quote: str, chunks: Sequence[ChunkLike], document_id: str) -> SourceRef | None:
    """Найти цитату во фрагментах и вернуть точное место.

    Сравнение нечёткое только по пробелам и регистру: этого достаточно
    для переносов строк, но недостаточно, чтобы «подтвердить» другую
    формулировку.
    """
    needle = " ".join((quote or "").split()).lower()
    if len(needle) < 3:
        return None
    for chunk in chunks:
        haystack = " ".join((chunk.text or "").split()).lower()
        if needle in haystack:
            return SourceRef(
                document_id=document_id,
                page=chunk.page,
                paragraph=chunk.paragraph,
                quote=quote,
            )
    return None


def _fact_id() -> str:
    return f"F-{uuid.uuid4().hex[:12]}"


def build_registry(
    payload: ExtractionPayload,
    chunks: Sequence[ChunkLike],
    *,
    document_id: str = "",
) -> CaseRegistry:
    """Превратить ответ модели в реестр, проверив всё, что можно проверить."""
    registry = CaseRegistry(case_number=payload.case_number or None)
    blocking = False

    for participant in payload.participants:
        name = " ".join(participant.name.split())
        if not name:
            continue
        registry.participants.append(
            CaseParticipant(
                role=normalize_role(participant.role),
                name=name,
                inn=participant.inn or None,
                source=_locate(participant.quote, chunks, document_id),
            )
        )

    for amount in payload.amounts:
        value = parse_money(amount.value)
        if value is None:
            registry.missing.append(
                MissingInformation(
                    field=f"amount:{amount.kind or 'unknown'}",
                    reason=f"сумма не разобрана: {amount.value!r}",
                    blocking=True,
                )
            )
            blocking = True
            continue
        registry.amounts.append(
            CaseAmount(
                kind=amount.kind or "other",
                value=value,
                source=_locate(amount.quote, chunks, document_id),
            )
        )

    for item in payload.dates:
        value = parse_date(item.value)
        if value is None:
            registry.missing.append(
                MissingInformation(
                    field=f"date:{item.kind or 'unknown'}",
                    reason=f"дата не разобрана: {item.value!r}",
                    blocking=True,
                )
            )
            blocking = True
            continue
        registry.dates.append(
            CaseDate(
                kind=item.kind or "other",
                value=value,
                raw=item.value,
                source=_locate(item.quote, chunks, document_id),
            )
        )

    for claim in payload.claims:
        subject = " ".join(claim.subject.split())
        if not subject:
            continue
        amount = parse_money(claim.amount) if claim.amount else None
        if claim.amount and amount is None:
            registry.missing.append(
                MissingInformation(
                    field="claim:amount",
                    reason=f"сумма требования не разобрана: {claim.amount!r}",
                    blocking=True,
                )
            )
            blocking = True
        registry.claims.append(
            CaseClaim(
                subject=subject,
                amount=amount,
                source=_locate(claim.quote, chunks, document_id),
            )
        )

    for raw_fact in payload.facts:
        value = " ".join(raw_fact.text.split())
        if not value:
            continue
        source = _locate(raw_fact.quote, chunks, document_id)
        # Факт без найденного источника не считается подтверждённым:
        # именно так модель и «превращает» догадку в установленный факт.
        status = FactStatus.SOURCE_MATCHED if source else FactStatus.UNSUPPORTED
        registry.facts.append(
            CaseFact(
                fact_id=_fact_id(),
                fact_type=raw_fact.category or "other",
                value=value,
                source=source,
                status=status,
            )
        )

    has_data = bool(
        registry.participants
        or registry.amounts
        or registry.dates
        or registry.claims
        or registry.facts
    )
    if blocking or not has_data:
        registry.status = ExtractionStatus.NEEDS_REVIEW
    return registry


async def extract_facts(
    provider: JsonProvider,
    chunks: Sequence[ChunkLike],
    *,
    document_id: str = "",
    max_chars: int = MAX_CONTEXT_CHARS,
    attempts: int = MAX_ATTEMPTS,
) -> ExtractionRun:
    """Извлечь факты из фрагментов документов. Без исключений наружу."""
    context = _context(chunks, max_chars=max_chars)
    errors: list[str] = []
    if not context.strip():
        return ExtractionRun(
            registry=CaseRegistry(status=ExtractionStatus.NEEDS_REVIEW),
            status=ExtractionStatus.NEEDS_REVIEW,
            attempts=0,
            errors=["нет текста для извлечения"],
        )

    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=f"Материалы дела:\n\n{context}"),
        ],
        role=LLMRole.EXTRACTION,
        temperature=0.0,
        max_tokens=1500,
        json_mode=True,
        json_schema=ExtractionPayload.model_json_schema(),
        reasoning=False,
    )

    used = 0
    for attempt in range(1, max(1, attempts) + 1):
        used = attempt
        try:
            raw = await provider.generate_json(request, default={})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"попытка {attempt}: провайдер не ответил: {exc}")
            continue

        if not raw:
            errors.append(f"попытка {attempt}: пустой ответ")
            continue

        try:
            payload = ExtractionPayload.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"попытка {attempt}: схема не прошла проверку: {exc}")
            continue

        registry = build_registry(payload, chunks, document_id=document_id)
        status = (
            ExtractionStatus.OK
            if registry.status == ExtractionStatus.OK
            else ExtractionStatus.NEEDS_REVIEW
        )
        return ExtractionRun(
            registry=registry, status=status, attempts=used, errors=errors
        )

    return ExtractionRun(
        registry=CaseRegistry(status=ExtractionStatus.NEEDS_REVIEW),
        status=ExtractionStatus.NEEDS_REVIEW,
        attempts=used,
        errors=errors,
    )
