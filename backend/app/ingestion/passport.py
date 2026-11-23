"""Паспорт дела: единый источник фактов для детерминированного иска.

Паспорт — это JSON схемы case-passport/3.1, загруженный вместе с материалами
(файл 00-ПАСПОРТ-ДЕЛА.json) или введённый через API. Он читается ПЕРВЫМ:
материалы дела служат доказательственной базой и цитатами, а не источником
реквизитов шапки, цены иска и требований.

Деньги — целые копейки. Арифметические инварианты проверяются на входе:
паспорт с нарушенной арифметикой принимается со статусом needs_review и
списком проблем, но не «исправляется» молча.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_PREFIX = "case-passport/"
SUPPORTED_SCHEMA = "case-passport/3.1"
_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_MARKER_RE = re.compile(r"^\[[^\]]+ — по данным дела\]$")


class PassportIssue(BaseModel):
    """Проблема паспорта: код,severity и человекочитаемое объяснение."""

    code: str
    severity: Literal["P0", "P1"]
    reason: str


def is_marker(value: str) -> bool:
    """Значение — унифицированный маркер пробела, а не выдуманные данные."""
    return bool(_MARKER_RE.match((value or "").strip()))


def parse_ru_date(value: str) -> date | None:
    if not _DATE_RE.match(value or ""):
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        return None


class CaseBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    number: str = ""
    court_for_header: str
    city: str = ""
    court_address: str = ""
    judge: str = ""
    decision_date: str = ""


class PartyBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    full: str
    short: str = ""
    genitive: str = ""
    instrumental: str = ""
    address: str = ""
    phone: str = ""
    identifier: str = ""
    representative: str | None = None


class PartiesBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    plaintiff: PartyBlock
    defendant: PartyBlock


class MoneyBlock(BaseModel):
    """Все суммы — целые копейки."""

    model_config = ConfigDict(extra="allow")

    loan: int = Field(ge=0)
    repaid: int = Field(ge=0)
    principal: int = Field(ge=0)
    interest: int = Field(ge=0)
    fee_paid: int = Field(ge=0)
    price: int = Field(ge=0)
    total: int = Field(ge=0)


class InterestPeriod(BaseModel):
    model_config = ConfigDict(extra="allow")

    start: str = Field(alias="from")
    end: str = Field(alias="to")
    days: int = Field(ge=0)
    rate: float = Field(ge=0)
    year_days: int = Field(ge=365, le=366)
    amount_kopecks: int = Field(ge=0)


class InterestBlock(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    norm: str = "ст. 395 ГК РФ"
    start: str = Field(alias="from")
    end: str = Field(alias="to")
    days: int = Field(ge=0)
    base_kopecks: int = Field(ge=0)
    periods: list[InterestPeriod] = Field(default_factory=list)
    closure_applied: bool = False
    closure_k: float | None = None
    raw_sum_kopecks: int | None = None
    delta_kopecks: int | None = None


class ClaimItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    n: int = Field(ge=1)
    kind: str
    amount_kopecks: int = Field(ge=0)
    text: str
    period: tuple[str, str] | None = None


class ContractBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    number: str = ""
    date: str
    due_date: str
    interest_free: bool = True
    subject: str = "заём"


class PretrialBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    letter_id: str = ""
    letter_date: str = ""
    rpo: str = ""
    sent_date: str = ""
    received_date: str = ""
    term_days: int = 0
    term_end: str = ""
    mandatory_by_law: bool = False


class FeeReceiptBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    date: str
    amount_kopecks: int = Field(ge=0)


class EventBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    code: str
    fact: str = ""


class DocumentItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str
    title: str
    title_genitive: str = ""
    sheets: int = Field(default=1, ge=0)
    appendix: bool = False


class LawBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    material: list[str] = Field(default_factory=list)
    procedural: list[str] = Field(default_factory=list)


class DeviationItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    item: str
    resolution: str = ""
    severity: str = "P1"


class CasePassport(BaseModel):
    """Паспорт дела. Структурная валидация — pydantic, арифметика — метод ниже."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_id: str = Field(alias="schema")
    mode: Literal["ETALON", "CLEAN"] = "ETALON"
    case: CaseBlock
    parties: PartiesBlock
    money_kopecks: MoneyBlock
    interest: InterestBlock
    claims: list[ClaimItem] = Field(default_factory=list)
    contract: ContractBlock
    pretrial: PretrialBlock | None = None
    fee_receipt: FeeReceiptBlock
    events: list[EventBlock] = Field(default_factory=list)
    suit_date: str
    suit_rpo: str = ""
    law: LawBlock = Field(default_factory=LawBlock)
    documents: list[DocumentItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    deviations: list[DeviationItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_schema(self) -> CasePassport:
        if not self.schema_id.startswith(SCHEMA_PREFIX):
            raise ValueError(f"неизвестная схема паспорта: {self.schema_id!r}")
        return self

    # ── Арифметика и полнота ────────────────────────────────────────────
    def validate_invariants(self) -> list[PassportIssue]:
        issues: list[PassportIssue] = []
        money = self.money_kopecks

        def add(code: str, severity: Literal["P0", "P1"], reason: str) -> None:
            issues.append(PassportIssue(code=code, severity=severity, reason=reason))

        if money.principal != money.loan - money.repaid:
            add("И-1", "P0", "основной долг не равен займу минус возвраты")
        if money.price != money.principal + money.interest:
            add("И-3", "P0", "цена иска не равна долгу плюс проценты")
        if money.total != money.price + money.fee_paid:
            add("И-4", "P0", "итог не равен цене иска плюс госпошлина")
        if money.fee_paid >= money.price > 0:
            add("И-10", "P0", "госпошлина не может быть больше цены иска")
        if self.fee_receipt.amount_kopecks != money.fee_paid:
            add("И-7", "P0", "сумма в чеке не совпадает с суммой пошлины в паспорте")

        periods = self.interest.periods
        if periods:
            if sum(p.amount_kopecks for p in periods) != money.interest:
                add("И-2", "P0", "сумма процентов по периодам не равна итогу процентов")
            if sum(p.days for p in periods) != self.interest.days:
                add("И-5", "P0", "сумма дней по периодам не равна заявленному числу дней")
        start = parse_ru_date(self.interest.start)
        end = parse_ru_date(self.interest.end)
        if start is None or end is None:
            add("И-6", "P0", "период процентов задан не в формате ДД.ММ.ГГГГ")
        elif (end - start).days + 1 != self.interest.days:
            add("И-6", "P0", "число дней не соответствует границам периода")

        if self.claims:
            if sum(c.amount_kopecks for c in self.claims) != money.total:
                add("И-8", "P0", "сумма требований не равна итогу к взысканию")
        else:
            add("И-8", "P0", "требования не заданы")

        for label, value in (
            ("суд", self.case.court_for_header),
            ("истец", self.parties.plaintiff.full),
            ("ответчик", self.parties.defendant.full),
            ("дата иска", self.suit_date),
        ):
            if not value.strip():
                add("И-11", "P0", f"не заполнен обязательный слот: {label}")
        if "[" in self.case.court_for_header or "___" in self.case.court_for_header:
            add("И-12", "P0", "в наименовании суда плейсхолдер")
        if parse_ru_date(self.suit_date) is None:
            add("И-13", "P0", "дата иска задана не в формате ДД.ММ.ГГГГ")

        for label, value in (
            ("адрес истца", self.parties.plaintiff.address),
            ("адрес ответчика", self.parties.defendant.address),
        ):
            if not value.strip() or is_marker(value):
                add("GAP", "P1", f"пробел данных: {label}")

        if not any(d.appendix for d in self.documents):
            add("И-14", "P1", "в манифесте нет ни одного приложения к иску")
        return issues

    @property
    def status(self) -> str:
        issues = self.validate_invariants()
        return "needs_review" if any(i.severity == "P0" for i in issues) else "ok"

    def as_dict(self) -> dict[str, Any]:
        """Словарь для ClaimModel.from_passport (алиасы обратно в from/to/schema)."""
        return self.model_dump(mode="json", by_alias=True)


# ── Поиск паспорта в материалах ─────────────────────────────────────────
def looks_like_passport(text: str) -> bool:
    body = (text or "").strip()
    return body.startswith("{") and SCHEMA_PREFIX in body[:200]


def parse_passport(text: str) -> CasePassport | None:
    """Разобрать паспорт из текста документа. None — это не паспорт."""
    if not looks_like_passport(text):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return CasePassport.model_validate(data)
    except Exception:  # noqa: BLE001 - структурно не паспорт
        return None


def find_passport_in_documents(documents: dict[str, str]) -> CasePassport | None:
    """Первый документ-паспорт среди материалов дела."""
    for text in documents.values():
        passport = parse_passport(text)
        if passport is not None:
            return passport
    return None


def find_passport_in_pack(pack: Any) -> CasePassport | None:
    documents = getattr(pack, "documents", None) or {}
    if isinstance(documents, dict):
        return find_passport_in_documents(
            {str(k): str(v) for k, v in documents.items()}
        )
    return None
