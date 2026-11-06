"""Проверка источников и противоречий (этап 3 ТЗ, §7).

Главный принцип: факт не считается подтверждённым потому, что его вернула
модель. Нужна цитата, и цитата должна действительно быть в документе.

Различаются три состояния, а не два (§7.2):

* `exact` — цитата найдена дословно;
* `normalized` — совпадает после нормализации (пробелы, ё/е, тире, кавычки);
* `absent` — не найдена вовсе.

Свободное нечёткое сравнение намеренно не используется: «похожая фраза»
не доказывает, что в документе написано именно это.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.extraction.schemas import (
    CaseRegistry,
    Contradiction,
    ExtractionStatus,
    FactStatus,
)

#: Символы, которые OCR и вёрстка меняют чаще всего.
_DASHES = str.maketrans({"—": "-", "–": "-", "−": "-", "«": '"', "»": '"', "“": '"', "”": '"'})
_SPACES = re.compile(r"\s+")


class QuoteStatus(StrEnum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    ABSENT = "absent"


class EvidenceIssueType(StrEnum):
    QUOTE_ABSENT = "QUOTE_ABSENT"
    QUOTE_NORMALIZED = "QUOTE_NORMALIZED"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    SOURCE_MISSING = "SOURCE_MISSING"
    CONTRADICTION = "CONTRADICTION"


@dataclass(frozen=True)
class QuoteCheck:
    status: QuoteStatus
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class EvidenceIssue:
    type: EvidenceIssueType
    message: str
    blocking: bool = False
    fact_id: str | None = None


@dataclass
class EvidenceReport:
    exact: int = 0
    normalized: int = 0
    absent: int = 0
    issues: list[EvidenceIssue] = field(default_factory=list)

    @property
    def status(self) -> ExtractionStatus:
        blocking = any(issue.blocking for issue in self.issues)
        return ExtractionStatus.NEEDS_REVIEW if blocking else ExtractionStatus.OK


def normalize(text: str) -> str:
    """Нормализация для сравнения цитат.

    Меняются только символы, которые не несут смысла: регистр, пробелы,
    ё/е, тире и кавычки. Слова не трогаются.
    """
    lowered = (text or "").lower().replace("ё", "е").translate(_DASHES)
    return _SPACES.sub(" ", lowered).strip()


#: Минимальная длина цитаты. «ООО» или «долг» встречаются в любом
#: документе и ничего не доказывают.
MIN_QUOTE_LENGTH = 5


def check_quote(quote: str, text: str) -> QuoteCheck:
    """Найти цитату в тексте: дословно, после нормализации или никак."""
    needle = (quote or "").strip()
    if len(needle) < MIN_QUOTE_LENGTH or not text:
        return QuoteCheck(QuoteStatus.ABSENT)

    position = text.find(needle)
    if position >= 0:
        return QuoteCheck(QuoteStatus.EXACT, position, position + len(needle))

    normalized_text = normalize(text)
    normalized_quote = normalize(needle)
    if len(normalized_quote) >= MIN_QUOTE_LENGTH and normalized_quote in normalized_text:
        return QuoteCheck(QuoteStatus.NORMALIZED)
    return QuoteCheck(QuoteStatus.ABSENT)


def verify_registry(registry: CaseRegistry, texts: dict[str, str]) -> EvidenceReport:
    """Проверить цитаты реестра по исходным текстам документов.

    `texts` — полный текст каждого документа по его id. Если документа в
    словаре нет, это отдельная ошибка: нельзя проверить то, чего нет, и
    нельзя считать это подтверждением.
    """
    report = EvidenceReport()

    def check(quote: str, document_id: str) -> QuoteCheck:
        text = texts.get(document_id)
        if text is None:
            report.issues.append(
                EvidenceIssue(
                    type=EvidenceIssueType.DOCUMENT_NOT_FOUND,
                    message=f"документ {document_id} отсутствует при проверке",
                    blocking=True,
                )
            )
            return QuoteCheck(QuoteStatus.ABSENT)
        result = check_quote(quote, text)
        if result.status is QuoteStatus.EXACT:
            report.exact += 1
        elif result.status is QuoteStatus.NORMALIZED:
            report.normalized += 1
        else:
            report.absent += 1
        return result

    for fact in registry.facts:
        if fact.source is None or not fact.source.quote:
            fact.status = FactStatus.UNSUPPORTED
            report.issues.append(
                EvidenceIssue(
                    type=EvidenceIssueType.SOURCE_MISSING,
                    message=f"у факта нет источника: {fact.value[:80]}",
                    fact_id=fact.fact_id,
                )
            )
            continue
        result = check(fact.source.quote, fact.source.document_id)
        if result.status is QuoteStatus.EXACT:
            fact.status = FactStatus.VERIFIED
        elif result.status is QuoteStatus.NORMALIZED:
            fact.status = FactStatus.SOURCE_MATCHED
            report.issues.append(
                EvidenceIssue(
                    type=EvidenceIssueType.QUOTE_NORMALIZED,
                    message=f"цитата совпала после нормализации: {fact.value[:60]}",
                    fact_id=fact.fact_id,
                )
            )
        else:
            fact.status = FactStatus.UNSUPPORTED
            report.issues.append(
                EvidenceIssue(
                    type=EvidenceIssueType.QUOTE_ABSENT,
                    message=f"цитата не найдена: {fact.source.quote[:60]}",
                    fact_id=fact.fact_id,
                )
            )

    for item in [*registry.participants, *registry.amounts, *registry.dates, *registry.claims]:
        source = item.source
        if source is None or not source.quote:
            continue
        result = check(source.quote, source.document_id)
        if result.status is QuoteStatus.ABSENT:
            report.issues.append(
                EvidenceIssue(
                    type=EvidenceIssueType.QUOTE_ABSENT,
                    message=f"цитата не найдена: {source.quote[:60]}",
                )
            )

    if report.status is ExtractionStatus.NEEDS_REVIEW:
        registry.status = ExtractionStatus.NEEDS_REVIEW
    return report


def detect_contradictions(registry: CaseRegistry) -> list[Contradiction]:
    """Найти расхождения одного и того же показателя между записями.

    Суммы одного вида с разными значениями блокируют вывод: пока неясно,
    какая верна, взыскивать нельзя. Даты — нет: разные даты одного вида
    могут быть разными событиями, и это вопрос юридической оценки (§8.2).
    """
    contradictions: list[Contradiction] = []

    amounts: dict[str, list] = {}
    for amount in registry.amounts:
        amounts.setdefault(amount.kind, []).append(amount)
    for kind, items in amounts.items():
        # Сравнение по числу, а не по записи: «1000» и «1000,00» — одна
        # и та же сумма. Иначе реестр сообщал бы о противоречии там, где
        # его нет, и блокировал вывод без причины.
        unique: dict[Decimal, str] = {}
        for item in items:
            unique.setdefault(item.value, str(item.value))
        if len(unique) > 1:
            contradictions.append(
                Contradiction(
                    kind=f"amount:{kind}",
                    values=sorted(unique.values()),
                    sources=[item.source for item in items if item.source is not None],
                    blocking=True,
                )
            )

    dates: dict[str, list] = {}
    for item in registry.dates:
        dates.setdefault(item.kind, []).append(item)
    for kind, items in dates.items():
        values = {item.value.isoformat() for item in items}
        if len(values) > 1:
            contradictions.append(
                Contradiction(
                    kind=f"date:{kind}",
                    values=sorted(values),
                    sources=[item.source for item in items if item.source is not None],
                    blocking=False,
                )
            )

    return contradictions


def apply_contradictions(registry: CaseRegistry) -> list[Contradiction]:
    """Добавить найденные противоречия в реестр и ужесточить статус."""
    contradictions = detect_contradictions(registry)
    if not contradictions:
        return []

    registry.contradictions.extend(contradictions)
    if any(item.blocking for item in contradictions):
        registry.status = ExtractionStatus.NEEDS_REVIEW
    return contradictions
