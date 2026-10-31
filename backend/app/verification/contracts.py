"""Контракты проверки: утверждение, свидетельство, результат, статус.

Этап 2 ТЗ. Здесь только структуры и правила интерпретации — без
обращений к моделям, базе и сети. Смысл модуля: результат проверки
должен быть однозначно представим, сохраняем и сравним.

Ключевые решения:

* Статус утверждения и статус документа — разные вещи. Утверждение
  может быть `SUPPORTED` и одновременно `CONTRADICTED` другим
  документом; сводить это к одному числу нельзя (ТЗ §9.3).
* `VERIFIED_WITHIN_SCOPE` — не «документ верен», а «обязательные
  проверки выполнены, критических дефектов нет» (ТЗ §14.1).
* Отчёт, относящийся к другой версии текста, недействителен. Версии
  проекта неизменяемы, поэтому достаточно сравнить номер версии.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, Field


class ClaimStatus(StrEnum):
    """Статус отдельной проверки утверждения (ТЗ §9.3)."""

    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    VERIFICATION_ERROR = "VERIFICATION_ERROR"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class ClaimType(StrEnum):
    """Тип проверяемого утверждения. Нужен для выбора валидатора."""

    FACT = "FACT"
    AMOUNT = "AMOUNT"
    DATE = "DATE"
    LEGAL_REFERENCE = "LEGAL_REFERENCE"
    LEGAL_CONCLUSION = "LEGAL_CONCLUSION"
    PROCEDURAL = "PROCEDURAL"


class DocumentStatus(StrEnum):
    """Статус документа по результатам проверки (ТЗ §14)."""

    DRAFT = "DRAFT"
    VERIFYING = "VERIFYING"
    VERIFIED_WITHIN_SCOPE = "VERIFIED_WITHIN_SCOPE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class EvidenceReference(BaseModel):
    """Происхождение свидетельства.

    `exact=False` означает, что точная страница или абзац неизвестны.
    Выдумывать локализацию нельзя (ТЗ §7.4), поэтому точность хранится
    явно, а не подразумевается.
    """

    document: str
    quote: str = ""
    locator: str = ""
    exact: bool = True


class CheckResult(BaseModel):
    """Результат одной проверки одного утверждения."""

    check_id: str
    status: ClaimStatus
    validator: str
    reason: str = ""
    sources: list[str] = Field(default_factory=list)
    validator_version: str = "1"


class Claim(BaseModel):
    """Проверяемое утверждение проекта.

    `support_status` и `contradiction_status` независимы: подтверждение
    одним документом не отменяет противоречия с другим.
    """

    claim_id: str
    text: str
    claim_type: ClaimType
    section: str = ""
    materiality: str = "MEDIUM"
    evidence: list[EvidenceReference] = Field(default_factory=list)
    support_status: ClaimStatus = ClaimStatus.INSUFFICIENT_EVIDENCE
    contradiction_status: ClaimStatus = ClaimStatus.NOT_APPLICABLE
    results: list[CheckResult] = Field(default_factory=list)
    review_required: bool = False
    review_reason: str = ""

    @property
    def blocked(self) -> bool:
        """Критично ли утверждение для допуска документа."""
        return self.contradiction_status is ClaimStatus.CONTRADICTED or (
            self.support_status is ClaimStatus.CONTRADICTED
        )


class ScopeReport(BaseModel):
    """Отчёт проверки, привязанный к конкретной версии текста."""

    draft_version: int
    text_sha256: str
    claims: list[Claim] = Field(default_factory=list)
    status: DocumentStatus = DocumentStatus.DRAFT

    @property
    def critical_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.blocked]

    @property
    def review_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.review_required]


def text_checksum(text: str) -> str:
    """Контрольная сумма текста версии.

    Нужна, чтобы отчёт нельзя было применить к изменённому тексту даже
    при совпадении номера версии (например, после ручной правки в базе).
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def is_stale(report_version: int, current_version: int) -> bool:
    """Устарел ли отчёт: относится ли он к другой версии документа."""
    return report_version != current_version


def decide_status(
    *,
    checks_failed: bool = False,
    stale: bool = False,
    critical: int = 0,
    review: int = 0,
    verified_checks: int = 0,
) -> DocumentStatus:
    """Итоговый статус документа по прозрачным правилам (ТЗ §14.1).

    Порядок важен: технический сбой и устаревание перекрывают любые
    «успешные» показатели. Документ не может считаться проверенным,
    если обязательная проверка не выполнялась.
    """
    if checks_failed:
        return DocumentStatus.VERIFICATION_FAILED
    if stale:
        return DocumentStatus.REVIEW_REQUIRED
    if critical > 0:
        return DocumentStatus.BLOCKED
    if verified_checks <= 0:
        # Проверок не было — это не «всё хорошо», а отсутствие проверки.
        return DocumentStatus.REVIEW_REQUIRED
    if review > 0:
        return DocumentStatus.REVIEW_REQUIRED
    return DocumentStatus.VERIFIED_WITHIN_SCOPE


#: Соответствие старым статусам проверки (models.entities.VerificationStatus).
#: Нужно, чтобы существующие записи в БД читались новым кодом без миграции.
_LEGACY_MAP = {
    "PASS": DocumentStatus.VERIFIED_WITHIN_SCOPE,
    "REVIEW_REQUIRED": DocumentStatus.REVIEW_REQUIRED,
    "BLOCKED": DocumentStatus.BLOCKED,
}


def from_legacy_status(value: str) -> DocumentStatus:
    """Перевести сохранённый статус проверки в статус документа."""
    return _LEGACY_MAP.get(str(value), DocumentStatus.REVIEW_REQUIRED)


def scope_report(
    *,
    draft_version: int,
    text: str,
    claims: list[Claim],
    checks_failed: bool = False,
    stale: bool = False,
) -> ScopeReport:
    """Собрать отчёт и посчитать итоговый статус по его содержимому."""
    status = decide_status(
        checks_failed=checks_failed,
        stale=stale,
        critical=len([c for c in claims if c.blocked]),
        review=len([c for c in claims if c.review_required]),
        verified_checks=len([c for c in claims if c.results]),
    )
    return ScopeReport(
        draft_version=draft_version,
        text_sha256=text_checksum(text),
        claims=claims,
        status=status,
    )
