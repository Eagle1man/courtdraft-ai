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
from decimal import Decimal
from typing import Protocol

from app.extraction.deterministic import extract_amounts
from app.extraction.schemas import (
    AmountsPayload,
    CaseAmount,
    CaseClaim,
    CaseDate,
    CaseFact,
    CaseParticipant,
    CaseRegistry,
    ClaimsPayload,
    DatesPayload,
    ExtractionPayload,
    ExtractionStatus,
    FactStatus,
    MissingInformation,
    ParticipantsPayload,
    SourceRef,
    normalize_party,
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


#: Слова, по которым фрагмент считается относящимся к категории. Это не
#: «умный поиск», а дешёвый отбор: без него в контекст попадают первые
#: 6 000 знаков документа, а суммы и требования часто оказываются в конце.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "participants": ("истец", "ответчик", "третье лицо", "представитель", "ооо", "ип "),
    "amounts": ("руб", "взыскать", "цена иска", "пени", "процент", "госпошлин", "долг"),
    "dates": ("дата", "срок", "договор", "20", "г."),
    "claims": ("просит", "взыскать", "требовани", "обязать", "признать"),
}


def _context_for(
    category: str,
    chunks: Sequence[ChunkLike],
    *,
    max_chars: int,
) -> str:
    """Отобрать фрагменты под конкретную категорию.

    Фрагменты с ключевыми словами идут первыми, остальные — как запас,
    если бюджет не заполнен. Порядок внутри выдачи сохраняется: цитата
    должна открываться в том месте документа, где она была.
    """
    keywords = CATEGORY_KEYWORDS.get(category, ())
    scored: list[tuple[int, int, ChunkLike]] = []
    for index, chunk in enumerate(chunks):
        text = (chunk.text or "").lower()
        score = sum(1 for word in keywords if word in text)
        scored.append((-score, index, chunk))
    scored.sort()

    selected: list[ChunkLike] = []
    used = 0
    for _negative, _index, chunk in scored:
        text = (chunk.text or "").strip()
        if not text:
            continue
        if selected and used + len(text) > max_chars:
            continue
        selected.append(chunk)
        used += len(text)
        if used >= max_chars:
            break

    order = {id(chunk): index for index, chunk in enumerate(chunks)}
    selected.sort(key=lambda item: order[id(item)])
    return _context(selected, max_chars=max_chars)


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
                party=normalize_party(raw_fact.party),
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
    mode: str = "single",
) -> ExtractionRun:
    """Извлечь факты из фрагментов документов. Без исключений наружу.

    `mode="single"` — один вызов со всей схемой, `mode="categories"` —
    отдельные вызовы по категориям (ТЗ §14.2). Разница измеряется, а не
    выбирается на веру.
    """
    context = _context(chunks, max_chars=max_chars)
    if not context.strip():
        return ExtractionRun(
            registry=CaseRegistry(status=ExtractionStatus.NEEDS_REVIEW),
            status=ExtractionStatus.NEEDS_REVIEW,
            attempts=0,
            errors=["нет текста для извлечения"],
        )

    if mode == "categories":
        return await _extract_by_categories(
            provider,
            chunks,
            max_chars=max_chars,
            document_id=document_id,
            attempts=attempts,
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

    errors: list[str] = []
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


#: Инструкции по категориям. Каждая — узкая задача: маленькой модели проще
#: заполнить короткую схему, чем всю сразу.
CATEGORY_PROMPTS: dict[str, tuple[str, type]] = {
    "participants": (
        "Извлеки только участников дела: истца, ответчика, третьих лиц, "
        "представителей.\n"
        "Поля: role (истец/ответчик/третье лицо/представитель), name "
        "(название организации или ФИО), inn (ИНН, если есть, иначе пусто), "
        "quote (дословная фраза из текста).\n"
        'Пример: {"participants": [{"role": "истец", "name": "ООО «Ромашка»", '
        '"inn": "", "quote": "Истец ООО «Ромашка»"}]}',
        ParticipantsPayload,
    ),
    "amounts": (
        "Извлеки только денежные суммы: цена иска, основной долг, пени, "
        "проценты, оплаты, остаток долга, судебные расходы.\n"
        "Поля: kind (principal/penalty/interest/paid/debt/costs/total), "
        "value (только число, как в документе, например \"1 000,00\"), "
        "quote (дословная фраза, где эта сумма указана).\n"
        'Пример: {"amounts": [{"kind": "principal", "value": "1 000,00", '
        '"quote": "задолженность в размере 1 000,00 руб."}]}',
        AmountsPayload,
    ),
    "dates": (
        "Извлеки только даты: договор, срок исполнения, события, платежи, "
        "дата обращения в суд.\n"
        "Поля: kind (contract/due/event/payment/filing), value (дата как в "
        "документе, например \"15.01.2025\"), quote (дословная фраза).\n"
        'Пример: {"dates": [{"kind": "contract", "value": "15.01.2025", '
        '"quote": "договор поставки от 15.01.2025"}]}',
        DatesPayload,
    ),
    "claims": (
        "Извлеки только требования истца: что просит, на какую сумму, на "
        "каком основании.\n"
        "Поля: subject (что просит), amount (сумма или пусто), quote "
        "(дословная фраза).\n"
        'Пример: {"claims": [{"subject": "взыскать задолженность", '
        '"amount": "1 000,00", "quote": "просит взыскать 1 000,00 руб."}]}',
        ClaimsPayload,
    ),
}


async def _extract_by_categories(
    provider: JsonProvider,
    chunks: Sequence[ChunkLike],
    *,
    max_chars: int,
    document_id: str,
    attempts: int,
) -> ExtractionRun:
    """По одному вызову на категорию, затем сборка общего реестра."""
    merged: dict[str, list] = {
        "participants": [],
        "amounts": [],
        "dates": [],
        "claims": [],
    }
    errors: list[str] = []
    calls = 0

    for category, (instructions, schema) in CATEGORY_PROMPTS.items():
        context = _context_for(category, chunks, max_chars=max_chars)

        # Суммы ищет Python: модель на реальных исках возвращает пустой
        # список, хотя числа в тексте есть. Это ровно тот случай, когда
        # детерминированный код надёжнее языковой модели (§8.1).
        if category == "amounts":
            combined = "\n\n".join((chunk.text or "") for chunk in chunks)
            found = extract_amounts(combined, document_id=document_id)
            if found:
                merged["amounts"].extend(
                    {
                        "kind": item.kind,
                        "value": str(item.value),
                        "quote": item.source.quote if item.source else "",
                    }
                    for item in found
                )
                continue

        answer: dict = {}
        for attempt in range(1, max(1, attempts) + 1):
            calls += 1
            request = LLMRequest(
                messages=[
                    LLMMessage(role="system", content=instructions),
                    LLMMessage(role="user", content=f"Материалы дела:\n\n{context}"),
                ],
                role=LLMRole.EXTRACTION,
                temperature=0.0,
                max_tokens=900,
                json_mode=True,
                json_schema=schema.model_json_schema(),
                reasoning=False,
            )
            try:
                raw = await provider.generate_json(request, default={})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{category}, попытка {attempt}: {exc}")
                continue
            if not raw:
                errors.append(f"{category}, попытка {attempt}: пустой ответ")
                continue
            try:
                parsed = schema.model_validate(raw)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{category}, попытка {attempt}: схема: {exc}")
                continue
            answer = parsed.model_dump()
            break

        for key in merged:
            merged[key].extend(answer.get(key) or [])

    payload = ExtractionPayload.model_validate(merged)
    registry = build_registry(payload, chunks, document_id=document_id)
    if errors:
        registry.status = ExtractionStatus.NEEDS_REVIEW
    status = (
        ExtractionStatus.OK
        if registry.status == ExtractionStatus.OK
        else ExtractionStatus.NEEDS_REVIEW
    )
    return ExtractionRun(
        registry=registry, status=status, attempts=calls, errors=errors
    )


def merge_registries(registries: Sequence[CaseRegistry]) -> CaseRegistry:
    """Собрать один реестр из реестров отдельных документов.

    Один и тот же участник и одна и та же сумма встречаются в нескольких
    документах дела (истец — в иске и в договоре). Дубли схлопываются по
    смыслу, а не по строке: роль и наименование для участников, вид и
    значение для сумм и дат. Противоречия при этом не теряются — их ищет
    `detect_contradictions` уже по объединённому реестру.
    """
    merged = CaseRegistry()
    seen_participants: set[tuple[str, str]] = set()
    seen_amounts: set[tuple[str, Decimal]] = set()
    seen_dates: set[tuple[str, str]] = set()

    for registry in registries:
        if not merged.case_number and registry.case_number:
            merged.case_number = registry.case_number
        for participant in registry.participants:
            key = (participant.role, " ".join(participant.name.lower().split()))
            if key not in seen_participants:
                seen_participants.add(key)
                merged.participants.append(participant)
        for amount in registry.amounts:
            # Ключ — само число, а не его запись: 1000 и 1000.00 в базе
            # и в документах означают одну сумму.
            key = (amount.kind, amount.value)
            if key not in seen_amounts:
                seen_amounts.add(key)
                merged.amounts.append(amount)
        for item in registry.dates:
            key = (item.kind, item.value.isoformat())
            if key not in seen_dates:
                seen_dates.add(key)
                merged.dates.append(item)
        merged.claims.extend(registry.claims)
        merged.facts.extend(registry.facts)
        merged.missing.extend(registry.missing)

    if any(item.status is not ExtractionStatus.OK for item in registries):
        merged.status = ExtractionStatus.NEEDS_REVIEW
    return merged
