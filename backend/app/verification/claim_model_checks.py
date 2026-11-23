"""Пятнадцать кросс-проверок иска, собранного из паспорта.

Проверки детерминированные: сравниваются модель, отрендеренный текст и
паспорт. Ни одна не обращается к языковой модели. Результат совместим с
VerificationReport движка, поэтому сохраняется и показывается существующим
кодом без изменений в схеме БД.

P0 блокирует выгрузку. P1 требует внимания, но выгрузку не запрещает.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.drafting.claim_model import ClaimModel
from app.ingestion.passport import CasePassport, parse_ru_date
from app.money import parse_money
from app.textnorm import broken_numbers, broken_words
from app.verification.contracts import DocumentStatus, decide_status
from app.verification.engine import (
    Issue,
    IssueType,
    Severity,
    VerificationReport,
    VerificationStatus,
)
from app.verification.jurisdiction import expected_tier

#: Код проверки → тип замечания движка. Новые члены enum не вводятся:
#: отчёты старых версий должны читаться тем же кодом.
_ISSUE_TYPE: dict[str, IssueType] = {
    "X01": IssueType.CONTRADICTION,
    "X02": IssueType.EXACT_TOTAL_MISMATCH,
    "X03": IssueType.UNSUPPORTED_CLAIM,
    "X04": IssueType.CONTRADICTION,
    "X05": IssueType.CONTRADICTION,
    "X06": IssueType.EXACT_TOTAL_MISMATCH,
    "X07": IssueType.FEE_IN_CLAIM_PRICE,
    "X08": IssueType.ATTACHMENT_MISSING,
    "X09": IssueType.JURISDICTION_MISMATCH,
    "X10": IssueType.LIMITATION_EXPIRED,
    "X11": IssueType.CONTRADICTION,
    "X12": IssueType.FORBIDDEN_OUTPUT,
    "X13": IssueType.INCOMPLETE,
    "X14": IssueType.FORBIDDEN_OUTPUT,
    "X15": IssueType.INCOMPLETE,
}

_SEVERITY: dict[str, Severity] = {
    "P0": Severity.CRITICAL,
    "P1": Severity.MEDIUM,
}

REQUIRED_CHECKS: tuple[str, ...] = tuple(f"X{n:02d}" for n in range(1, 16))

_PRICE_RE = re.compile(r"^Цена иска:\s*(.+)$", re.MULTILINE)
_FEE_RE = re.compile(r"^Государственная пошлина:\s*(.+)$", re.MULTILINE)
_TOTAL_RE = re.compile(r"^Всего к взысканию:\s*(.+)$", re.MULTILINE)
_PERIOD_RE = re.compile(r"с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})")
_ATTACH_LINE_RE = re.compile(r"^\s*\d+\.\s+.*?—\s*(\d+)\s*л\.", re.MULTILINE)
_ATTACH_TOTAL_RE = re.compile(r"Всего:\s*(\d+)\s+\S+\s+на\s+(\d+)\s+лист")
_META_MARKERS: tuple[str, ...] = (
    "вот проект",
    "вот текст",
    "текст оборван",
    "проект подготовлен программой",
    "требует проверки",
    "подлежит проверке",
    "mock-ответ",
    "[наименование",
    "«___»",
    "____ г.",
    ".docx",
    "таблица 1:",
)
_ROLE_INVERSION: tuple[str, ...] = ("вам ", "взыскать с истца")


def _role_inversion_found(low: str) -> str | None:
    """Проверка инверсии ролей: 'вами' как слово, не подстрока 'средствами'."""
    for marker in _ROLE_INVERSION:
        if marker in low:
            return marker
    if re.search(r'\bвами\b', low):
        return "вами"
    return None


@dataclass(frozen=True, slots=True)
class CheckFinding:
    """Одно срабатывание кросс-проверки."""

    code: str
    severity: str  # P0 | P1
    reason: str
    fragment: str = ""

    def to_issue(self) -> Issue:
        return Issue(
            type=_ISSUE_TYPE.get(self.code, IssueType.INCOMPLETE),
            severity=_SEVERITY.get(self.severity, Severity.MEDIUM),
            text=self.fragment[:300],
            reason=f"{self.code}: {self.reason}",
            section="claim",
        )


@dataclass
class ClaimVerification:
    """Итог проверки иска: находки, счётчики и статус документа."""

    findings: list[CheckFinding] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)

    @property
    def p0_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "P0")

    @property
    def p1_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "P1")

    @property
    def document_status(self) -> DocumentStatus:
        missing = [c for c in REQUIRED_CHECKS if c not in self.executed]
        return decide_status(
            checks_failed=bool(missing),
            critical=self.p0_count,
            review=self.p1_count,
            verified_checks=len(self.executed),
        )

    def to_report(self, *, sections_checked: int = 1) -> VerificationReport:
        status = (
            VerificationStatus.BLOCKED
            if self.p0_count
            else (
                VerificationStatus.REVIEW_REQUIRED
                if self.findings
                else VerificationStatus.PASS
            )
        )
        report = VerificationReport(
            status=status,
            issues=[f.to_issue() for f in self.findings],
            sections_checked=sections_checked,
            claims_checked=len(self.executed),
            unsupported_claims=len(self.findings),
        )
        report.claim_proof_summary = {  # type: ignore[attr-defined]
            "document_kind": "claim",
            "p0_count": self.p0_count,
            "p1_count": self.p1_count,
            "checks_executed": list(self.executed),
            "document_status": self.document_status.value,
        }
        return report


def _first_amount(line: str) -> int | None:
    tokens = parse_money(line)
    return tokens[0].kopecks if tokens else None


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 29 февраля
        return value.replace(year=value.year + years, day=28)


def verify_claim_model(
    model: ClaimModel,
    text: str,
    *,
    passport: CasePassport | None = None,
    mode: str = "draft",
    today: date | None = None,
) -> ClaimVerification:
    """Пятнадцать проверок. Каждая выполняется всегда и отмечается в executed."""
    out = ClaimVerification()
    body = text or ""

    def add(code: str, severity: str, reason: str, fragment: str = "") -> None:
        out.findings.append(CheckFinding(code, severity, reason, fragment))

    def done(code: str) -> None:
        out.executed.append(code)

    # X01. Цена иска = долг + проценты (модель и паспорт).
    done("X01")
    if model.price != model.principal + model.interest:
        add("X01", "P0", "цена иска не равна сумме долга и процентов")
    if passport is not None:
        for issue in passport.validate_invariants():
            if issue.code.startswith("И-"):
                add("X01", issue.severity, f"паспорт: {issue.reason}")

    # X02. Итог = цена иска + пошлина; сумма требований = итог.
    done("X02")
    if model.total != model.price + model.fee_paid:
        add("X02", "P0", "итог не равен цене иска плюс госпошлина")
    demands_sum = sum(d.amount_kopecks for d in model.demands)
    if demands_sum != model.total:
        add("X02", "P0", f"сумма требований {demands_sum} не равна итогу {model.total}")
    total_line = _TOTAL_RE.search(body)
    if total_line is None:
        add("X02", "P0", "в тексте нет строки «Всего к взысканию»")
    elif _first_amount(total_line.group(1)) != model.total:
        add("X02", "P0", "итог в тексте не совпадает с моделью", total_line.group(0))

    # X03. Пошлина в тексте = уплаченная по чеку.
    done("X03")
    fee_line = _FEE_RE.search(body)
    if fee_line is None:
        add("X03", "P0", "в тексте нет строки с государственной пошлиной")
    else:
        stated = _first_amount(fee_line.group(1))
        if stated != model.fee_paid:
            add("X03", "P0", "пошлина в тексте не совпадает с уплаченной", fee_line.group(0))
    if passport is not None and passport.fee_receipt.amount_kopecks != model.fee_paid:
        add("X03", "P0", "пошлина не совпадает с суммой чека")

    # X04. Период процентов в тексте = период расчёта.
    done("X04")
    expected_period = (model.interest_from, model.interest_to)
    found_periods = _PERIOD_RE.findall(body)
    if not found_periods:
        add("X04", "P0", "в тексте не найден период начисления процентов")
    for start, end in found_periods:
        if (start, end) != expected_period:
            add("X04", "P0", f"период {start}–{end} не совпадает с расчётом")
    if "по дату подачи иска" in body.lower():
        add("X04", "P0", "период процентов задан открытой формулировкой")

    # X05. Шапка суда: точное совпадение и единственное вхождение.
    done("X05")
    header_first = model.court_for_header.split("\n")[0]
    key = (
        header_first.split(" Центрального")[0]
        if " Центрального" in header_first
        else header_first
    )
    if body.count(key) != 1:
        add("X05", "P0", f"наименование суда встречается {body.count(key)} раз, ожидался 1")
    if not body.lstrip().startswith(header_first.split(" Центрального")[0][:20]):
        add("X05", "P0", "документ не начинается с наименования суда")

    # X06. Арифметика расчёта процентов.
    done("X06")
    if passport is not None:
        periods = passport.interest.periods
        if periods:
            if sum(p.amount_kopecks for p in periods) != model.interest:
                add("X06", "P0", "сумма строк расчёта не равна итогу процентов")
            if sum(p.days for p in periods) != model.interest_days:
                add("X06", "P0", "сумма дней расчёта не равна периоду")
        else:
            add("X06", "P1", "в паспорте нет построчного расчёта процентов")

    # X07. Госпошлина не входит в цену иска.
    done("X07")
    if model.price == model.total and model.fee_paid > 0:
        add("X07", "P0", "цена иска включает государственную пошлину (ст. 91 ГПК РФ)")
    price_line = _PRICE_RE.search(body)
    if price_line is None:
        add("X07", "P0", "в тексте нет строки «Цена иска»")
    elif _first_amount(price_line.group(1)) != model.price:
        add("X07", "P0", "цена иска в тексте не совпадает с моделью", price_line.group(0))

    # X08. Приложения: число позиций и сумма листов.
    done("X08")
    block = body.split("Приложение:")[-1] if "Приложение:" in body else ""
    sheets = [int(v) for v in _ATTACH_LINE_RE.findall(block)]
    if len(sheets) != len(model.attachments):
        add("X08", "P0", f"в тексте {len(sheets)} приложений, в модели {len(model.attachments)}")
    if sum(sheets) != model.attachments_sheets:
        add("X08", "P0", f"сумма листов {sum(sheets)} не равна {model.attachments_sheets}")
    stated_total = _ATTACH_TOTAL_RE.search(block)
    if stated_total is None:
        add("X08", "P0", "нет строки «Всего: N наименований на M листах»")
    elif int(stated_total.group(2)) != model.attachments_sheets:
        add("X08", "P0", "итог листов в тексте не совпадает с моделью")

    # X09. Подсудность по цене иска.
    done("X09")
    expected = expected_tier(model.price / 100)
    court_low = model.court_for_header.lower()
    actual = "мировой судья" if "миров" in court_low or "участок" in court_low else "районный суд"
    if expected != actual:
        add(
            "X09",
            "P1",
            f"при цене иска {model.price / 100:,.2f} руб. подсуден {expected}, "
            f"а в шапке {actual} (ст. 23, 24 ГПК РФ)",
        )

    # X10. Исковая давность.
    done("X10")
    due = parse_ru_date(model.due_date)
    filed = parse_ru_date(model.suit_date)
    if due is None or filed is None:
        add("X10", "P1", "даты для проверки давности заданы некорректно")
    elif filed > _add_years(due, 3):
        add("X10", "P1", "срок исковой давности истёк (ст. 196 ГК РФ)")

    # X11. Инверсия ролей и обращение к ответчику.
    done("X11")
    low = body.lower()
    surname = model.defendant.full.split()[0].lower() if model.defendant.full else ""
    if surname and f"я, {surname}" in low:
        add("X11", "P0", "в иске от истца цитата от первого лица ответчика")
    marker = _role_inversion_found(low)
    if marker is not None:
        add("X11", "P0", f"недопустимая для иска формулировка: «{marker.strip()}»")

    # X12. Разрывы чисел и слов.
    done("X12")
    for raw in broken_numbers(body):
        add("X12", "P0", f"разрыв внутри числа: {raw}", raw)
    for raw in broken_words(body):
        add("X12", "P1", f"возможен разрыв слова: {raw}", raw)

    # X13. Обязательные слоты.
    done("X13")
    for label, value in (
        ("суд", model.court_for_header),
        ("истец", model.plaintiff.full),
        ("ответчик", model.defendant.full),
        ("дата иска", model.suit_date),
    ):
        if not value.strip():
            add("X13", "P0", f"не заполнен обязательный слот: {label}")
    if not model.demands:
        add("X13", "P0", "требования не заданы")
    if not model.attachments:
        add("X13", "P0", "приложения не заданы")
    address_severity = "P0" if mode == "final" else "P1"
    for label, value in (
        ("адрес истца", model.plaintiff.address),
        ("адрес ответчика", model.defendant.address),
    ):
        if not value.strip() or value.strip().startswith("["):
            add("X13", address_severity, f"пробел данных: {label}")

    # X14. Служебный текст, маркеры, плейсхолдеры, имена файлов.
    done("X14")
    for marker in _META_MARKERS:
        if marker in low:
            add("X14", "P0", f"в тексте служебная формулировка: «{marker}»")
    if "|" in body:
        add("X14", "P0", "в тексте табличная разметка вместо нативной таблицы")

    # X15. Объём документа.
    done("X15")
    length = len(body)
    if length < 3200:
        add("X15", "P1", f"объём {length} знаков: иск неполон (ожидается 3200–4500)")
    elif length > 4500:
        add("X15", "P1", f"объём {length} знаков: иск многословен (ожидается 3200–4500)")

    return out
