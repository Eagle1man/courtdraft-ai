"""Типизированная модель иска. Единственный источник текста для render_claim."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any


class ClaimModelError(ValueError):
    """Модель не собрана: данные паспорта противоречивы или неполны."""


@dataclass(frozen=True, slots=True)
class Party:
    full: str
    short: str
    genitive: str
    instrumental: str
    address: str
    phone: str = ""


@dataclass(frozen=True, slots=True)
class Demand:
    n: int
    kind: str
    amount_kopecks: int
    text: str
    period: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    title: str
    title_genitive: str
    sheets: int


@dataclass(frozen=True, slots=True)
class ClaimModel:
    court_for_header: str
    court_address: str
    plaintiff: Party
    defendant: Party
    loan: int
    repaid: int
    principal: int
    interest: int
    fee_paid: int
    price: int
    total: int
    contract_number: str
    contract_date: str
    due_date: str
    interest_free: bool
    interest_from: str
    interest_to: str
    interest_days: int
    partial_date: str
    payment_order: str
    pretrial_letter_id: str
    pretrial_letter_date: str
    pretrial_rpo: str
    pretrial_sent: str
    pretrial_received: str
    pretrial_term_days: int
    pretrial_term_end: str
    suit_date: str
    demands: tuple[Demand, ...]
    attachments: tuple[Attachment, ...]
    material_norms: tuple[str, ...]
    procedural_norms: tuple[str, ...]
    subject_kind: str = "loan"
    mode: str = "ETALON"

    # ── Сборка ────────────────────────────────────────────────────────
    @classmethod
    def from_passport(cls, data: dict[str, Any]) -> ClaimModel:
        try:
            money = data["money_kopecks"]
            case = data["case"]
            contract = data["contract"]
            interest = data["interest"]
            pretrial = data["pretrial"]
            plaintiff = _party(data["parties"]["plaintiff"])
            defendant = _party(data["parties"]["defendant"])
        except KeyError as exc:
            raise ClaimModelError(f"в паспорте нет обязательного блока: {exc}") from exc

        events = {str(e.get("code")): str(e.get("date")) for e in data.get("events", [])}
        documents = data.get("documents", [])
        attachments = tuple(
            Attachment(
                title=str(d.get("title", "")),
                title_genitive=str(d.get("title_genitive") or d.get("title", "")),
                sheets=int(d.get("sheets", 1)),
            )
            for d in documents if d.get("appendix")
        )
        demands = tuple(
            Demand(
                n=int(c["n"]),
                kind=str(c["kind"]),
                amount_kopecks=int(c["amount_kopecks"]),
                text=str(c["text"]),
                period=(tuple(c["period"]) if c.get("period") else None),
            )
            for c in sorted(data.get("claims", []), key=lambda c: int(c["n"]))
        )
        model = cls(
            court_for_header=str(case["court_for_header"]),
            court_address=str(case.get("court_address", "")),
            plaintiff=plaintiff,
            defendant=defendant,
            loan=int(money["loan"]),
            repaid=int(money["repaid"]),
            principal=int(money["principal"]),
            interest=int(money["interest"]),
            fee_paid=int(money["fee_paid"]),
            price=int(money["price"]),
            total=int(money["total"]),
            contract_number=str(contract.get("number", "")),
            contract_date=str(contract["date"]),
            due_date=str(contract["due_date"]),
            interest_free=bool(contract.get("interest_free", True)),
            interest_from=str(interest["from"]),
            interest_to=str(interest["to"]),
            interest_days=int(interest["days"]),
            partial_date=events.get("PARTIAL", str(contract["due_date"])),
            payment_order=_payment_order(documents, data.get("events", [])),
            pretrial_letter_id=str(pretrial["letter_id"]),
            pretrial_letter_date=str(pretrial["letter_date"]),
            pretrial_rpo=str(pretrial["rpo"]),
            pretrial_sent=str(pretrial["sent_date"]),
            pretrial_received=str(pretrial["received_date"]),
            pretrial_term_days=int(pretrial["term_days"]),
            pretrial_term_end=str(pretrial["term_end"]),
            suit_date=str(data["suit_date"]),
            demands=demands,
            attachments=attachments,
            material_norms=tuple(data.get("law", {}).get("material", [])),
            procedural_norms=tuple(data.get("law", {}).get("procedural", [])),
            subject_kind="loan" if "заём" in str(contract.get("subject", "")) else "other",
            mode=str(data.get("mode", "ETALON")),
        )
        model.validate()
        return model

    # ── Инварианты (все в копейках, целыми) ───────────────────────────
    def validate(self) -> None:
        if self.principal != self.loan - self.repaid:
            raise ClaimModelError("И-1: основной долг не равен займу минус возвраты")
        if self.price != self.principal + self.interest:
            raise ClaimModelError("И-3: цена иска не равна долгу плюс проценты")
        if self.total != self.price + self.fee_paid:
            raise ClaimModelError("И-4: итог не равен цене иска плюс пошлина")
        if self.fee_paid >= self.price:
            raise ClaimModelError("И-10: пошлина не может входить в цену иска")
        if not self.demands:
            raise ClaimModelError("требования не заданы")
        if sum(d.amount_kopecks for d in self.demands) != self.total:
            raise ClaimModelError("сумма требований не равна итогу к взысканию")
        if not self.attachments:
            raise ClaimModelError("приложения не заданы")
        for slot, value in (("суд", self.court_for_header), ("истец", self.plaintiff.full),
                            ("ответчик", self.defendant.full), ("дата иска", self.suit_date)):
            if not value:
                raise ClaimModelError(f"не заполнен обязательный слот: {slot}")
        if "[" in self.court_for_header or "___" in self.court_for_header:
            raise ClaimModelError("в наименовании суда плейсхолдер")

    # ── Правка = замена слота, не патч текста ─────────────────────────
    def with_court(self, value: str) -> ClaimModel:
        return replace(self, court_for_header=value)

    @property
    def attachments_sheets(self) -> int:
        return sum(a.sheets for a in self.attachments)


def _party(raw: dict[str, Any]) -> Party:
    return Party(
        full=str(raw["full"]),
        short=str(raw.get("short", "")),
        genitive=str(raw.get("genitive") or raw["full"]),
        instrumental=str(raw.get("instrumental") or raw["full"]),
        address=str(raw.get("address", "")),
        phone=str(raw.get("phone", "")),
    )


def _payment_order(documents: list[dict[str, Any]], events: list[dict[str, Any]]) -> str:
    """Реквизиты платёжного поручения — из манифеста документа, затем из события."""
    for doc in documents:
        title = str(doc.get("title", ""))
        low = title.lower()
        if not low.startswith("платёжное поручение") and not low.startswith("платежное поручение"):
            continue
        number = re.search(r"№\s*(\d+)", title)
        date = re.search(r"от\s+(\d{2}\.\d{2}\.\d{4})", title)
        if number and date:
            return f"№ {number.group(1)} от {date.group(1)}"
    for event in events:
        if str(event.get("code")) != "PARTIAL":
            continue
        number = re.search(r"п/п\s*№\s*(\d+)", str(event.get("fact", "")))
        if number:
            return f"№ {number.group(1)} от {event.get('date')}"
    raise ClaimModelError("реквизиты платёжного поручения не найдены в паспорте")
