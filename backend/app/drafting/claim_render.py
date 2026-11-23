"""Чистый рендер иска из ClaimModel. Без LLM, без БД, без правки строк."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.drafting.claim_model import ClaimModel
from app.money import format_rub
from app.money_words import plural
from app.textnorm import broken_numbers, normalize

SIGNATURE_GAP = " " * 24
_MARKER_RE = re.compile(r"\[[^\]]*требует проверки[^\]]*\]|ТРЕБУЕТ ПРОВЕРКИ", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RenderedSection:
    key: str
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class RenderedClaim:
    text: str
    sections: tuple[RenderedSection, ...] = ()
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def p0_count(self) -> int:
        return len(broken_numbers(self.text)) + len(_MARKER_RE.findall(self.text))


def render_claim(model: ClaimModel) -> RenderedClaim:
    """Один модель → один текст. Побайтово воспроизводимо."""
    model.validate()
    blocks: list[tuple[str, str, str]] = [
        ("header", "Шапка и адресат", _court_block(model)),
        ("plaintiff", "Истец", _plaintiff_block(model)),
        ("defendant", "Ответчик", _defendant_block(model)),
        ("price", "Цена иска и госпошлина", _price_block(model)),
        ("title", "Наименование иска", _title_block(model)),
    ]
    for index, paragraph in enumerate(_facts(model), start=1):
        blocks.append((f"fact{index}", f"Обстоятельство {index}", paragraph))
    blocks += [
        ("legal_basis", "Правовое обоснование", _legal_basis(model)),
        ("preamble", "Основание обращения", _preamble(model)),
        ("request_head", "Просительная часть", "ПРОШУ:"),
        ("request", "Требования", _petitum(model)),
        ("total", "Всего к взысканию",
         f"Всего к взысканию: {format_rub(model.total, style='words')}."),
        ("attachments", "Приложения", _attachments(model)),
        ("signature", "Дата и подпись", _signature(model)),
    ]
    text = normalize("\n\n".join(body for _key, _name, body in blocks))
    sections = tuple(RenderedSection(key, name, body) for key, name, body in blocks)
    findings = tuple(
        f"пробел данных: {label}"
        for label, value in (
            ("адрес истца", model.plaintiff.address),
            ("адрес ответчика", model.defendant.address),
            ("телефон истца", model.plaintiff.phone),
            ("адрес суда", model.court_address),
        )
        if value.startswith("[")
    )
    return RenderedClaim(text=text, sections=sections, findings=findings)


# ── Блоки ───────────────────────────────────────────────────────────────
def _court_block(model: ClaimModel) -> str:
    lines = _court_lines(model.court_for_header)
    if model.court_address:
        lines.append(model.court_address)
    return "\n".join(lines)


def _court_lines(header: str) -> list[str]:
    """Мировой судья: номер участка — первой строкой, район и город — второй."""
    match = re.match(r"^(.*?судебного участка №\s*\d+)\s+(.*)$", header)
    if match:
        return [match.group(1), match.group(2)]
    return [header]


def _plaintiff_block(model: ClaimModel) -> str:
    lines = [f"Истец: {model.plaintiff.full}", f"адрес: {model.plaintiff.address}"]
    if model.plaintiff.phone:
        lines.append(f"телефон: {model.plaintiff.phone}")
    return "\n".join(lines)


def _defendant_block(model: ClaimModel) -> str:
    return f"Ответчик: {model.defendant.full}\nадрес: {model.defendant.address}"


def _price_block(model: ClaimModel) -> str:
    return (f"Цена иска: {format_rub(model.price, style='words')}\n"
            f"Государственная пошлина: {format_rub(model.fee_paid, style='words')}")


def _title_block(model: ClaimModel) -> str:
    return f"ИСКОВОЕ ЗАЯВЛЕНИЕ\n{_subject(model)}"


def _subject(model: ClaimModel) -> str:
    kinds = {d.kind for d in model.demands}
    if model.subject_kind == "loan":
        if "principal" in kinds and "interest" in kinds:
            return ("о взыскании задолженности по договору займа и процентов "
                    "за пользование чужими денежными средствами")
        if "principal" in kinds:
            return "о взыскании задолженности по договору займа"
    return "о взыскании задолженности"


def _contract_label(model: ClaimModel) -> str:
    number = model.contract_number or "б/н"
    return f"договор займа {number}"


def _facts(model: ClaimModel) -> list[str]:
    loan_words = format_rub(model.loan, style="words")
    repaid_words = format_rub(model.repaid, style="words")
    principal_words = format_rub(model.principal, style="words")
    interest_words = format_rub(model.interest, style="words")
    principal_plain = format_rub(model.principal, style="plain")

    first = (
        f"{model.contract_date} между мной, {model.plaintiff.instrumental} (Займодавец), "
        f"и {model.defendant.instrumental} (Заёмщик) заключён {_contract_label(model)}, "
        f"по условиям которого я передала ответчику {loan_words} со сроком возврата "
        f"до {model.due_date}. Передача денежных средств подтверждается собственноручной "
        f"распиской ответчика от {model.contract_date}."
    )
    if model.interest_free:
        first += " Проценты за пользование суммой займа договором не предусмотрены."

    second = (
        f"{model.partial_date} ответчик частично возвратил заём — {repaid_words}, "
        f"что подтверждается платёжным поручением {model.payment_order}. "
        f"Остаток основного долга составил {principal_words} и до настоящего времени "
        f"не возвращён."
    )
    third = (
        f"{model.pretrial_letter_date} я направила ответчику претензию {model.pretrial_letter_id} "
        f"с требованием погасить остаток долга в течение {model.pretrial_term_days} календарных "
        f"дней со дня получения (РПО № {model.pretrial_rpo} от {model.pretrial_sent}). "
        f"Претензия вручена {model.pretrial_received}, срок добровольного исполнения истёк "
        f"{model.pretrial_term_end}, требование оставлено без удовлетворения."
    )
    fourth = (
        f"За пользование чужими денежными средствами на сумму долга {principal_plain} "
        f"за период с {model.interest_from} по {model.interest_to} ({model.interest_days} дней) "
        f"начислены проценты по ст. 395 ГК РФ в размере {interest_words} согласно прилагаемому "
        f"расчёту, произведённому по ключевой ставке Банка России, действовавшей "
        f"в соответствующие периоды."
    )
    return [first, second, third, fourth]


def _legal_basis(model: ClaimModel) -> str:
    if model.subject_kind != "loan":
        norms = ", ".join(n.replace("ст. ", "") for n in model.material_norms)
        return f"Требования основаны на нормах: {norms}."
    return (
        "В силу ст.ст. 309, 310 ГК РФ обязательства должны исполняться надлежащим образом, "
        "односторонний отказ от исполнения не допускается. Согласно ст.ст. 807, 808, 810 "
        "ГК РФ заёмщик обязан возвратить сумму займа в установленный срок; факт заключения "
        "договора и передачи денег подтверждён распиской. На сумму просроченного долга "
        "подлежат начислению проценты по ст. 395 ГК РФ. Уплаченная государственная пошлина "
        "относится к судебным расходам и возмещается стороной, в пользу которой состоялось "
        "решение (ст. 98 ГПК РФ)."
    )


def _norms_group(norms: tuple[str, ...], code: str) -> str:
    numbers = [n.replace("ст. ", "").replace(f" {code}", "").strip() for n in norms]
    return f"ст.ст. {', '.join(numbers)} {code}"


def _preamble(model: ClaimModel) -> str:
    material = _norms_group(model.material_norms, "ГК РФ")
    procedural = _norms_group(model.procedural_norms, "ГПК РФ")
    return f"На основании изложенного, руководствуясь {material}, {procedural},"


def _petitum(model: ClaimModel) -> str:
    lines: list[str] = []
    for demand in model.demands:
        period = ""
        if demand.period:
            period = f" за период с {demand.period[0]} по {demand.period[1]}"
        amount = format_rub(demand.amount_kopecks, style="words")
        lines.append(
            f"{demand.n}. Взыскать с {model.defendant.genitive} в пользу "
            f"{model.plaintiff.genitive} {demand.text}{period} в размере {amount}."
        )
    return "\n".join(lines)


def _attachments(model: ClaimModel) -> str:
    lines = ["Приложение:"]
    for index, attachment in enumerate(model.attachments, start=1):
        if attachment.title_genitive.lower().startswith(("расчёт", "расчет")):
            title = attachment.title
            prefix = ""
        else:
            title = attachment.title_genitive
            prefix = "Копия "
        lines.append(f"{index}. {prefix}{title} — {attachment.sheets} л.")
    count = len(model.attachments)
    sheets = model.attachments_sheets
    names = plural(count, ("наименование", "наименования", "наименований"))
    sheets_word = "листе" if sheets == 1 else "листах"
    lines.append(f"Всего: {count} {names} на {sheets} {sheets_word}.")
    return "\n".join(lines)


def _signature(model: ClaimModel) -> str:
    short = model.plaintiff.short or model.plaintiff.full
    return f"{model.suit_date}{SIGNATURE_GAP}Истец: _____________ / {short} /"
