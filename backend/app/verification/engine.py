"""Verification Engine (Фаза 10, ТЗ п.28, п.29).

Проверяет сгенерированный проект на соответствие материалам дела.

Ключевая идея: проверка НЕ доверяет модели. Модель в 1.5B на CPU
выдумывает суммы и факты чаще, чем пишет хорошо, поэтому все числа
сверяются с пакетом доказательств регулярками, а не словами модели
о том, что «всё соответствует материалам».

Проверки:
- числа в тексте, которых нет в материалах (главный источник ошибок);
- ссылки на нормы права без соответствующего блока в материалах;
- разделы, помеченные моделью как требующие проверки;
- разделы, для которых в материалах нет данных вообще;
- разделы без единой ссылки на источник.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.agents.draft_agent import NEEDS_REVIEW, DraftResult
from app.agents.evidence_pack import MONTHS, EvidenceItem, EvidencePack
from app.config import Settings
from app.drafting.operative import DEFAULT_LEGAL_BASIS
from app.drafting.requirements import extract_requirements
from app.verification.evidence_assessment import (
    AssessmentCriterion,
    assessment_of,
    check_evidence_assessment,
)
from app.verification.jurisdiction import check_jurisdiction
from app.verification.laws import LawRegistry, extract_fee, fee_matches, state_fee
from app.verification.persons import PersonFinding, RoleFinding, check_section
from app.verification.qualification import check_qualification
from app.verification.resol_checks import check_resolution
from app.verification.subject_of_proof import ProofFact, subject_of_proof


class VerificationStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - статус проверки, не пароль
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"


class IssueType(StrEnum):
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    MISSING_SOURCE = "MISSING_SOURCE"
    INCORRECT_QUOTE = "INCORRECT_QUOTE"
    LAW_NOT_FOUND = "LAW_NOT_FOUND"
    PRACTICE_NOT_FOUND = "PRACTICE_NOT_FOUND"
    CONTRADICTION = "CONTRADICTION"
    INCOMPLETE = "INCOMPLETE"
    JURISDICTION_MISMATCH = "JURISDICTION_MISMATCH"
    CONTRACT_QUALIFICATION = "CONTRACT_QUALIFICATION"
    ARBITRATION_JURISDICTION = "ARBITRATION_JURISDICTION"
    OBJECTIONS_UNADDRESSED = "OBJECTIONS_UNADDRESSED"
    PRETRIAL_UNADDRESSED = "PRETRIAL_UNADDRESSED"
    FEE_IN_CLAIM_PRICE = "FEE_IN_CLAIM_PRICE"
    STATEMENT_LIMITATION = "STATEMENT_LIMITATION"
    STATEMENT_PENALTY_REDUCTION = "STATEMENT_PENALTY_REDUCTION"
    EVIDENCE_NOT_ASSESSED = "EVIDENCE_NOT_ASSESSED"
    PLEADING_AS_EVIDENCE = "PLEADING_AS_EVIDENCE"
    EXPERT_UNASSESSED = "EXPERT_UNASSESSED"
    CLAIM_AMOUNT_UNPARSED = "CLAIM_AMOUNT_UNPARSED"
    CITATION_DANGLING = "CITATION_DANGLING"
    PENALTY_BASIS_UNCONFIRMED = "PENALTY_BASIS_UNCONFIRMED"
    # Проверки резолютивной части (app/verification/resol_checks.py):
    # имена значений совпадают с id срабатываний датасета фикстур.
    RESOL_NO_COSTS = "RESOL_NO_COSTS"
    RESOL_COSTS_MERGED = "RESOL_COSTS_MERGED"
    RESOL_TOTAL_MISMATCH = "RESOL_TOTAL_MISMATCH"
    RESOL_NO_AMOUNT = "RESOL_NO_AMOUNT"
    RESOL_FULLY_PARTIALLY = "RESOL_FULLY_PARTIALLY"
    RESOL_NO_WHOM = "RESOL_NO_WHOM"
    RESOL_NO_ID = "RESOL_NO_ID"
    RESOL_NO_SHARES = "RESOL_NO_SHARES"
    RESOL_NO_APPEAL = "RESOL_NO_APPEAL"
    RESOL_NO_MOTIVATED = "RESOL_NO_MOTIVATED"
    RESOL_NO_IMMEDIATE = "RESOL_NO_IMMEDIATE"
    RESOL_NO_DEADLINE = "RESOL_NO_DEADLINE"
    RESOL_NO_PROPERTY_VALUE = "RESOL_NO_PROPERTY_VALUE"
    RESOL_ZAOCHNO_NO_OTMENA_ORDER = "RESOL_ZAOCHNO_NO_OTMENA_ORDER"
    RESOL_ALIMONY_NO_INDEXATION = "RESOL_ALIMONY_NO_INDEXATION"
    RESOL_ALIMONY_NO_PERIOD = "RESOL_ALIMONY_NO_PERIOD"
    RESOL_NO_CONSUMER_FINE = "RESOL_NO_CONSUMER_FINE"
    RESOL_PROPORTION_NON_PROPERTY = "RESOL_PROPORTION_NON_PROPERTY"
    RESOL_PROPORTION_REDUCED = "RESOL_PROPORTION_REDUCED"
    RESOL_FEE_FROM_EXEMPT = "RESOL_FEE_FROM_EXEMPT"
    RESOL_MOTIVATION_IN_OPERATIVE = "RESOL_MOTIVATION_IN_OPERATIVE"
    RESOL_WRONG_FEE_EDITION = "RESOL_WRONG_FEE_EDITION"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class Issue:
    """Замечание по проверке."""

    type: IssueType
    severity: Severity
    text: str
    reason: str
    section: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "text": self.text[:300],
            "reason": self.reason,
            "section": self.section,
        }


@dataclass
class VerificationReport:
    """Результат проверки проекта."""

    status: VerificationStatus = VerificationStatus.PASS
    issues: list[Issue] = field(default_factory=list)
    sections_checked: int = 0
    claims_checked: int = 0
    unsupported_claims: int = 0
    # Таблица «обстоятельство — доказательства» по предмету доказывания.
    # Это не замечание, а карта: судье виднее, что поставками считать,
    # поэтому в статус проверки не входит.
    facts: list[ProofFact] = field(default_factory=list)
    # Критерии оценки доказательств (ст. 67 ГПК РФ), найденные в
    # проекте. Тоже карта, а не замечание: оценку выносит суд.
    assessment: list[AssessmentCriterion] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.status is not VerificationStatus.PASS

    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.severity.value] = counts.get(issue.severity.value, 0) + 1
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "issues": [issue.to_dict() for issue in self.issues],
            "sections_checked": self.sections_checked,
            "claims_checked": self.claims_checked,
            "unsupported_claims": self.unsupported_claims,
            "by_severity": self.by_severity(),
            "subject_of_proof": [f.to_dict() for f in self.facts],
            "evidence_assessment": [c.to_dict() for c in self.assessment],
        }

    def human_summary(self) -> str:
        # Неподтверждённые обстоятельства показываются и без замечаний:
        # таблица предмета доказывания — самостоятельная ценность, и
        # «Замечаний нет» на пустой таблице прятало бы пробел дела.
        uncovered = [f for f in self.facts if not f.covered]
        if not self.issues and not uncovered:
            return "Замечаний нет: все разделы опираются на материалы дела."

        lines = (
            [f"Статус проверки: {self.status.value}", ""]
            if self.issues
            else ["Замечаний по разделам нет.", ""]
        )
        for issue in self.issues:
            lines.append(
                f"[{issue.severity.value}] {issue.section or 'проект целиком'}\n"
                f"  {issue.type.value}: {issue.reason}\n"
                f"  фрагмент: {issue.text[:140]}"
            )
        if self.issues:
            counts = self.by_severity()
            lines.append("")
            lines.append(
                "Итого: " + ", ".join(f"{k} — {v}" for k, v in sorted(counts.items()))
            )
        uncovered = [f for f in self.facts if not f.covered]
        if uncovered:
            lines.append("")
            lines.append("Ни одним документом не подтверждено (предмет доказывания):")
            for fact in uncovered:
                lines.append(f"  — {fact.circumstance}")
        return "\n".join(lines)


# Числа, которые встречаются в тексте проекта.
_NUM_RE = re.compile(r"\b(\d[\d\s ]{1,12})\b")
# Ссылки на нормы. Учитываются падежные формы: «статья 310», «статьи 310»,
# «статье 310», «статью 310», «ст. 310», «п. 1 ст. 486». Без «статье»
# ссылка не распознавалась вовсе, и выдуманная норма проходила проверку.
_LAW_RE = re.compile(
    # «ст.» и «статья N» имеют приоритет над «п. N»: иначе «п. 1 ст. 486»
    # распознаётся как «пункт 1 статьи СТ», то есть как выдуманная норма.
    #
    # Падежные формы перечислены полностью, включая «статьей»: без неё
    # ссылка не распознавалась, а её номер статьи тут же попадал в
    # проверку чисел и помечался как выдуманная сумма — то есть
    # корректная ссылка на норму давала ложную тревогу CRITICAL.
    r"(?:ст\.|стат(?:ьёй|ьей|ье|ьи|ью|ьями|ьях|ьям|ьё|и)\.?)\s*"
    r"(\d+(?:\s*(?:,|и)\s*\d+)*)"
    r"\s*(?:([А-ЯЁ][А-ЯЁ ]{0,12}?)(?:РФ|кодекс))?",
    re.IGNORECASE,
)

#: Госпошлина в цене иска: «150 000 руб., включая госпошлину».
#: Ч. 1 ст. 91 ГПК РФ относит госпошлину к судебным расходам, а не
#: к цене иска: цена определяется по требованию без пошлины, и
#: «включая» завышает цену и расчёт пошлины (ст. 333.19 НК РФ).
#: Между префиксом и «пошлин» допускается до 25 символов: «госпошлину»,
#: «государственную пошлину», «государственную пошлину (НК РФ)».
_FEE_IN_PRICE_RE = re.compile(
    r"включ\w*\s+[^.]{0,25}пошлин"
    r"|в\s+том\s+числе\s+[^.]{0,25}пошлин"
    r"|с\s+уч[её]том\s+[^.]{0,25}пошлин",
    re.IGNORECASE,
)


def _numbers_in(text: str) -> set[str]:
    """Числа в тексте, пригодные для сверки с материалами.

    Пропускаются реквизиты: ИНН, ОГРН, номера счетов — длинные
    последовательности с ведущими нулями («000000000000», «002»).
    Это не суммы, и проверка не должна кричать «выдуманное число»
    на каждый ИНН в тексте.
    """
    out: set[str] = set()
    for raw in _NUM_RE.findall(text):
        digits = re.sub(r"[^\d]", "", raw)
        if len(digits) < 2:
            continue
        # Ведущие нули — признак реквизита, а не суммы.
        if len(digits) > 1 and digits[0] == "0":
            continue
        if set(digits) == {"0"}:
            continue
        out.add(digits)
    return out


_MONTH_INDEX: dict[str, int] = {name: i + 1 for i, name in enumerate(MONTHS)}


def _case_date(dates: list[str]) -> date | None:
    """Дата, ближайшая к обращению в суд.

    pack.dates хранит формат «5 марта 2013 года», из которого понятно
    только слово и число. Редакция госпошлины выбирается по дате
    обращения в суд, а не по сегодняшнему дню: реальные дела корпуса —
    2012–2013 годов, и нынешний тариф к ним неприменим. Из дат
    материалов берётся самая ранняя — дата иска обычно раньше всех
    остальных дат дела, и это лучшая доступная оценка, а не решение
    о дате обращения.
    """
    parsed: list[date] = []
    for raw in dates:
        m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", str(raw))
        if m is None:
            continue
        month = _MONTH_INDEX.get(m.group(2))
        if month is None:
            continue
        try:
            parsed.append(date(int(m.group(3)), month, int(m.group(1))))
        except ValueError:
            continue
    return min(parsed) if parsed else None


def _laws_in(text: str) -> set[tuple[str, str]]:
    """Найти ссылки на нормы в виде (статья, кодекс).

    Кодекс определяется по ближайшему упоминанию после номера статьи.
    Если кодекс не указан прямо, ищем в окне контекста — иначе ссылка
    «статья 310» останется без кодекса и не с чем сравнивать.
    """
    known = ("ГПК", "ГК", "УК", "АПК", "ТК", "СК", "КАС", "КоАП")
    found: set[tuple[str, str]] = set()

    for m in _LAW_RE.finditer(text):
        # «статьями 309, 310, 314 ГК РФ» — это три ссылки, а не одна.
        # «статьями 309, 310 и 314 ГК РФ» — это три ссылки, а не одна.
        # Список разделяется и запятой, и союзом «и»: модель пишет и так,
        # и так, а приведённая форма без разбора давала «310 и 314» одним
        # номером статьи, и обе ссылки уходили в проверку чисел.
        articles = [
            a.strip()
            for a in re.split(r"\s*(?:,|и)\s*", m.group(1))
            if a.strip()
        ]
        code = (m.group(2) or "").strip().upper()

        if not code:
            tail = text[m.end() : m.end() + 40].upper()
            for name in known:
                if name in tail:
                    code = name
                    break

        for article in articles:
            # Без кодекса ссылку невозможно подтвердить: она всегда будет
            # отмечена как неподтверждённая. Это правильное поведение —
            # безопаснее перестраховаться, чем пропустить выдуманную норму.
            found.add((article, code or ""))

    return found


def _is_sum_of(digits: str, backed: set[int]) -> bool:
    """Сумма ли это из двух-трёх чисел, уже подтверждённых материалами.

    Проверяется только сумма двух и трёх слагаемых: этого хватает для
    строки «а всего» и для процентов по ключевой ставке, а перебор всех
    подмножеств при большом числе сумм дал бы неверные совпадения.

    Числа в тексте собраны без ведущих нулей, поэтому и сравнение идёт
    по целым значениям, а не по строкам.
    """
    try:
        target = int(digits)
    except ValueError:
        return False

    values = sorted(backed)
    for index, first in enumerate(values):
        if first == target:
            # Сумма из одного слагаемого — это не сумма: такое число
            # должно быть подтверждено материалами напрямую.
            return False
        for second in values[index + 1 :]:
            if first + second == target:
                return True
            for third in values[index + 2 :]:
                if first + second + third == target:
                    return True
    return False


#: Нормы, которые система приводит сама по закону.
#:
#: Смысл в том, что подтверждать их материалами дела бессмысленно:
#: в исковом заявлении судья ст. 198 ГПК не упоминает, она появляется
#: в самом акте суда. До появления этого списка проверка объявляла
#: нормативную часть собственной резолюции неподтверждённой — три
#: замечания HIGH на проекте, собранном по правилам.
SYSTEM_LAWS: frozenset[tuple[str, str]] = frozenset(
    {
        ("198", "ГПК"),  # основание принятия решения
        ("98", "ГПК"),  # распределение судебных расходов, пошлина в бюджет
        ("100", "ГПК"),  # расходы на представителя
        ("233", "ГПК"),  # заочное решение
        ("237", "ГПК"),  # обжалование заочного: отмена (ч. 1) и апелляция (ч. 2)
        ("321", "ГПК"),  # срок подачи апелляционной жалобы
    }
)


#: Дата вида 15.04.2025 — так в материалах дела записаны почти все даты.
_RU_DATE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
#: Дата рядом с претензией: «Претензия от 01.09.2025» и тому подобное.
_PRETRIAL_CLAIM_DATE = re.compile(r"претензи\w*\D{0,60}?(\d{2}\.\d{2}\.\d{4})", re.I)
#: Дата рядом со словом об исполнении: «оплатил 05.09.2025», «возврат от 10.09.2025».
_SATISFACTION_DATE = re.compile(
    r"(?:оплат|возврат|возвращ|перечисл|удовлетвор|исполн)\D{0,60}?(\d{2}\.\d{2}\.\d{4})",
    re.I,
)
#: ЗоЗПП, ст. 22: требование потребителя удовлетворяется в десятидневный срок,
#: по его истечении отказ добровольно не удовлетворять считается подтверждённым.
_PENALTY_WINDOW_DAYS = 10


def _parse_ru_date(raw: str) -> date | None:
    """Дата из материалов дела, если записана в форме 15.04.2025."""
    try:
        day, month, year = (int(part) for part in raw.split("."))
        return date(year, month, day)
    except ValueError:
        return None


def _resol_meta(
    pack: EvidencePack, stated: float | None, claim_total: float
) -> dict[str, Any]:
    """Метаданные для проверок резолютивной — только то, что видно в материалах.

    Неизвестное остаётся ложным: правило без метаданных молчит, а не угадывает.
    Льгота пошлины (claimant_exempt) в материалах дела не читается — пока False,
    и проверка FEE_FROM_EXEMPT молчит до тех пор, пока льгота не станет явью.
    """
    category = (pack.category or "").lower()
    return {
        "claim_date": _case_date(pack.dates),
        "claim_price": claim_total,
        "paid_fee": stated,
        "consumer_case": "потребител" in category,
        "claimant_exempt": False,
        "default_judgment": "заочн" in category,
    }


def _check_penalty_basis(
    text: str, pack: EvidencePack, report: VerificationReport, section_name: str
) -> None:
    """Штраф в резолютивной против материалов дела.

    Штраф (п. 6 ст. 13 ЗоЗПП) взыскивается, только если ответчик
    добровольно не удовлетворил требование. Но «не найдено» не равно
    «не было»: дефект разметки неотличим от отсутствия документа,
    поэтому отсутствие претензии — это MEDIUM, а не HIGH.
    """
    if not re.search(r"\bштраф", text, re.I):
        return
    claimed = [
        name
        for name, doc in pack.documents.items()
        if re.search(r"претензи", name, re.I) or re.search(r"претензи", doc, re.I)
    ]
    if not claimed:
        report.issues.append(
            Issue(
                type=IssueType.PENALTY_BASIS_UNCONFIRMED,
                severity=Severity.MEDIUM,
                text=text[:200],
                reason=(
                    "штраф начислен; по материалам претензия не найдена — "
                    "проверьте, заявлялось ли требование до суда"
                ),
                section=section_name,
            )
        )
        return
    claim_on = None
    for name in claimed:
        near = _PRETRIAL_CLAIM_DATE.search(pack.documents[name])
        first = _RU_DATE.search(pack.documents[name])
        raw = near.group(1) if near else (first.group(0) if first else "")
        claim_on = _parse_ru_date(raw) if raw else None
        if claim_on:
            break
    if claim_on is None:
        return
    for name, doc in pack.documents.items():
        if name in claimed:
            continue
        done = _SATISFACTION_DATE.search(doc)
        if not done:
            continue
        satisfied = _parse_ru_date(done.group(1))
        if satisfied is None or not 0 <= (satisfied - claim_on).days <= _PENALTY_WINDOW_DAYS:
            continue
        report.issues.append(
            Issue(
                type=IssueType.PENALTY_BASIS_UNCONFIRMED,
                severity=Severity.HIGH,
                text=text[:200],
                reason=(
                    "штраф начислен при отсутствии подтверждённого отказа: "
                    f"претензия от {claim_on:%d.%m.%Y} исполнена {satisfied:%d.%m.%Y} "
                    "— в десятидневный срок (ЗоЗПП, ст. 22)"
                ),
                section=section_name,
            )
        )
        return


def verify_draft(
    draft: DraftResult, pack: EvidencePack, settings: Settings | None = None
) -> VerificationReport:
    """Проверить проект на соответствие материалам дела."""
    report = VerificationReport(sections_checked=len(draft.sections))

    # Числа и нормы, реально присутствующие в материалах.
    material_numbers: set[str] = set(pack.amounts)
    material_laws: set[tuple[str, str]] = set()
    for item in pack.items:
        material_numbers |= set(item.amounts)
        material_laws |= _laws_in(item.text)

    # Допускаем номера статей и даты из дат дела: они не суммы, но
    # встречаются в материалах и их отсутствие в пакете — ложное срабатывание.
    for date_value in pack.dates:
        material_numbers |= _numbers_in(date_value)
    material_numbers |= {re.sub(r"\D", "", p) for p in [pack.case_number]}
    material_numbers.discard("")
    # Номера частей и пунктов встречаются почти везде.
    small_numbers = {str(i) for i in range(1, 50)}

    # Нормы, которые система цитирует сама, по закону, а не берёт из
    # материалов дела. Разбор 1 425 гражданских решений: ст. 198 ГПК —
    # основание принятия решения, 521 документ; «в доход бюджета» — 98
    # документов (6,9 %), ст. 233 — 87 (заочное решение), ст. 321 — 87
    # (срок обжалования). Ни одну из них сторона в иске не пишет, поэтому
    # требовать подтверждения материалами нельзя: система ругалась сама
    # на собственную резолютивную часть.
    system_laws = SYSTEM_LAWS | _laws_in(DEFAULT_LEGAL_BASIS)

    # База законодательства. Норма подтверждается ею, а не материалами
    # дела: суд ссылается на ст. 309 ГК, которой в иске нет, и требовать
    # её подтверждения материалами нельзя — так каждая верная ссылка
    # помечалась неподтверждённой.
    registry = LawRegistry.load(settings.laws_dir if settings else Path("data/laws"))

    # Цена иска — сумма денежных требований. Нужна для расчёта
    # госпошлины: по НК РФ пошлина считается от неё, а не берётся из
    # текста иска как есть.
    money_claims, _others, _unrecognised = extract_requirements(pack)
    claim_total = sum(
        float(claim.amount or 0) for claim in money_claims if claim.amount is not None
    )

    # F1: молчание хуже вопроса. Если в иске есть суммы, но ни одно
    # требование не извлечено — или извлечены только базы без сумм —
    # судья должен увидеть пробел, а не пустую резолютивную часть.
    if not any(claim.amount for claim in money_claims):
        claim_sums = {
            amount
            for item in pack.items
            if item.category == "claims" and item.amounts
            for amount in item.amounts
        }
        if claim_sums:
            report.issues.append(
                Issue(
                    type=IssueType.CLAIM_AMOUNT_UNPARSED,
                    severity=Severity.HIGH,
                    text="",
                    reason=(
                        "в материалах есть суммы требований, но ни одно "
                        "требование не извлечено в проект — проверить "
                        "вручную, какая сумма подлежит взысканию"
                    ),
                )
            )

    # D2: ссылка резолютивной части на документ, которого нет в материалах.
    known_documents = {
        item.source_document for item in pack.items if item.source_document
    }
    for section in draft.sections:
        if section.key != "operative":
            continue
        for citation in section.citations:
            base = citation.split(",")[0].strip()
            if base and base not in known_documents:
                report.issues.append(
                    Issue(
                        type=IssueType.CITATION_DANGLING,
                        severity=Severity.LOW,
                        text=citation,
                        reason=(
                            "ссылка на документ, которого нет в материалах "
                            "дела: возможно, опечатка в имени файла"
                        ),
                    )
                )

    # Родовая подсудность сверяется с судом из материалов: расхождение
    # между ценой иска и инстанцией — то, что судья должен увидеть до
    # чтения проекта, а не после.
    for jur_finding in check_jurisdiction(
        [item.text for item in pack.items], claim_total
    ):
        report.issues.append(
            Issue(
                type=IssueType.JURISDICTION_MISMATCH,
                severity=Severity.HIGH,
                text="",
                reason=jur_finding.reason,
            )
        )

    # Вопросы о квалификации договора и подведомственности (п. 3–4).
    # Система не решает, принадлежит ли спор арбитражу, но и не молчит:
    # признаки поставки, ИП и организации есть в материалах — вопрос
    # выносится судье наружу, а не остаётся внутри пакета.
    def _side_by_role(role: str) -> str:
        marker = f" ({role})"
        for entry in pack.participants:
            if entry.endswith(marker):
                return entry[: -len(marker)]
        return ""

    for qual_finding in check_qualification(
        [item.text for item in pack.items],
        plaintiff=_side_by_role("истец"),
        defendant=_side_by_role("ответчик"),
    ):
        report.issues.append(
            Issue(
                type=(
                    IssueType.ARBITRATION_JURISDICTION
                    if qual_finding.kind == "arbitration"
                    else IssueType.CONTRACT_QUALIFICATION
                ),
                severity=Severity.MEDIUM,
                text="",
                reason=qual_finding.reason,
            )
        )

    for section in draft.sections:
        text = section.text

        # ── Раздел-заглушка ──────────────────────────────────────
        #
        # Заглушка не содержит утверждений: в ней нет ни сумм, ни норм,
        # ни персон — только пометка о том, что модель не вызывалась.
        # Проверять её так же, как настоящий текст, бессмысленно и
        # вредно: номер в служебной пометке заглушки объявлялся
        # выдумкой модели с пометкой CRITICAL.
        #
        # Это выглядело как «система не работает», хотя честная причина
        # одна: раздел не сформирован. На демонстрации заказчику такая
        # проверка врёт дважды — выдумки не было и опасности нет.
        if section.is_mock:
            report.issues.append(
                Issue(
                    type=IssueType.UNSUPPORTED_CLAIM,
                    severity=Severity.MEDIUM,
                    text=text[:200],
                    reason=(
                        "раздел не сформирован: реальная модель не "
                        "вызвана, вместо текста — служебная заглушка"
                    ),
                    section=section.name,
                )
            )
            if section.error:
                report.issues.append(
                    Issue(
                        type=IssueType.INCOMPLETE,
                        severity=Severity.HIGH,
                        text=text[:200],
                        reason=f"ошибка генерации: {section.error}",
                        section=section.name,
                    )
                )
            continue

        report.claims_checked += 1

        # ── Пустой раздел ────────────────────────────────────────
        # Не заглушка, а молчаливо пустой текст: проверки ниже по
        # пустой строке ничего не находят, счётчики остаются нулевыми,
        # а судья видит в проекте «утверждений без источника: 0» рядом
        # с пустой вводной частью (п. 9.5 разбора). Пустота — это
        # результат тоже: о нём нужно сказать, иначе счётчики врут.
        if not text.strip():
            report.unsupported_claims += 1
            report.issues.append(
                Issue(
                    type=IssueType.INCOMPLETE,
                    severity=Severity.HIGH,
                    text="",
                    reason=(
                        f"раздел «{section.name}» пуст: проверять нечего, "
                        "данные из материалов в него не попали"
                    ),
                    section=section.name,
                )
            )
            continue

        # Номера статей из ссылок на нормы — не суммы. Без этого любая
        # корректная ссылка «статья 310 ГК РФ» помечалась бы как
        # «выдуманное число».
        law_numbers = {article for article, _ in _laws_in(text)}

        # ── Разделы, собранные правилами, а не моделью ────────────
        #
        # Проверки 1, 3 и 6 написаны для текста модели: выдуманное
        # число, выдуманная персона и отсутствие источника — это
        # признаки именно модели. В разделах, собранных правилами,
        # итог «а всего», копейки и госпошлина — арифметика проекта,
        # которой в материалах в готовом виде быть не может, а
        # «Именем Российской Федерации» — установленная форма, а не
        # персона. На живом прогоне это давало 8 CRITICAL на проекте
        # без единой выдумки, и настоящие тревоги тонули в ложных.
        by_rules = section.built_by == "rules"

        # ── 1. Раздел без единой ссылки на источник ────────────────
        if not by_rules and not section.citations and not pack.by_category(section.key):
            report.issues.append(
                Issue(
                    type=IssueType.MISSING_SOURCE,
                    severity=Severity.HIGH,
                    text=text[:200],
                    reason=(
                        "в материалах дела нет данных для раздела "
                        f"«{section.name}»"
                    ),
                    section=section.name,
                )
            )

        # ── 2. Раздел, помеченный моделью как требующий проверки ───
        if section.needs_review or NEEDS_REVIEW in text:
            report.issues.append(
                Issue(
                    type=IssueType.INCOMPLETE,
                    severity=Severity.MEDIUM,
                    text=text[:200],
                    reason="раздел помечен как требующий проверки специалистом",
                    section=section.name,
                )
            )

        # ── 3. Числа, которых нет в материалах (главная проверка) ──
        if by_rules:
            # Числа раздела собраны из материалов правилами: сверка
            # их арифметики — работа scripts/e2e_audit.py, а не
            # проверки «на выдумку модели».
            pass
        else:
            found_numbers = _numbers_in(text)
            # Числа, которые опираются на материалы дела. Сумма из них —
            # не выдумка, а арифметика проекта: «а всего» в резолюции
            # считается системой из уже подтверждённых сумм, и её не может
            # быть в материалах в готовом виде.
            backed = {
                int(number)
                for number in found_numbers
                if number in material_numbers
            }
            invented = {
                number
                for number in found_numbers
                if number not in material_numbers
                and number not in small_numbers
                and number not in law_numbers
                and not _is_sum_of(number, backed)
            }
            for number in sorted(invented):
                report.unsupported_claims += 1
                report.issues.append(
                    Issue(
                        type=IssueType.UNSUPPORTED_CLAIM,
                        severity=Severity.CRITICAL,
                        text=text[:200],
                        reason=(
                            f"число {number} отсутствует в материалах дела — "
                            "возможна выдумка модели"
                        ),
                        section=section.name,
                    )
                )

        # ── 4. Ссылки на нормы без подтверждения ───────────────────
        for article, code in _laws_in(text):
            if (article, code) in system_laws:
                # Норма, которую цитирует сама система по закону, а не
                # берёт из материалов дела. Подтверждать её материалами
                # бессмысленно: в иске судью ст. 198 ГПК не пишут.
                continue
            if code and registry.contains(code, article):
                # Статья существует в кодексе — ссылка законна, даже если
                # в материалах дела о ней не сказано. Суд применяет
                # норму, а не цитирует иск.
                continue
            if (article, code) in material_laws:
                continue
            report.issues.append(
                Issue(
                    type=IssueType.LAW_NOT_FOUND,
                    severity=Severity.HIGH,
                    text=text[:200],
                    reason=(
                        f"ссылка на ст. {article} {code} не подтверждена: "
                        f"{registry.missing(code, article) or 'нет в материалах дела'}"
                    ),
                    section=section.name,
                )
            )

        # ── 4b. Госпошлина против расчётной ──────────────────────
        # Пошлину система берёт из материалов дела, а она там может быть
        # указана неверно. По НК РФ (ст. 333.19) размер пошлины считается
        # от цены иска по редакции, действовавшей на дату обращения,
        # и расхождение — повод показать судье оба числа.
        if "резолютив" in section.name.lower():
            stated = extract_fee(text)
            fee_date = _case_date(pack.dates)
            if stated is not None and not fee_matches(
                claim_total, stated, on=fee_date
            ):
                edition = (
                    f"по редакции, действовавшей на {fee_date:%d.%m.%Y}"
                    if fee_date
                    else "по действующей редакции"
                )
                report.issues.append(
                    Issue(
                        type=IssueType.UNSUPPORTED_CLAIM,
                        severity=Severity.MEDIUM,
                        text=text[:200],
                        reason=(
                            f"госпошлина {stated:.2f} руб. не совпадает с "
                            f"расчётной по цене иска {claim_total:.2f} руб. "
                            f"{edition}: {state_fee(claim_total, fee_date):.2f} "
                            "руб. (НК РФ, ст. 333.19)"
                        ),
                        section=section.name,
                    )
                )

            _check_penalty_basis(text, pack, report, section.name)

            # Проверки резолютивной части: 22 текстовых правила, каждое
            # покрыто фикстурами (tests/data/resol_fixtures.json).
            for defect in check_resolution(text, _resol_meta(pack, stated, claim_total)):
                report.issues.append(
                    Issue(
                        type=IssueType[defect.id],
                        severity=Severity(defect.severity),
                        text=defect.fragment or text[:200],
                        reason=defect.reason,
                        section=section.name,
                    )
                )

        # ── 5. Раздел не удалось сгенерировать ─────────────────────
        if section.error:
            report.issues.append(
                Issue(
                    type=IssueType.INCOMPLETE,
                    severity=Severity.HIGH,
                    text=text[:200],
                    reason=f"ошибка генерации: {section.error}",
                    section=section.name,
                )
            )

        # ── 6. Персоны, которых нет в материалах дела ─────────────
        # В правилах-разделах формульные слова («Именем Российской
        # Федерации») за персону не считаются — см. пояснение к by_rules.
        if by_rules:
            persons: list[PersonFinding] = []
            roles: list[RoleFinding] = []
        else:
            persons, roles = check_section(text, section.name, pack)

        for finding in persons:
            report.unsupported_claims += 1
            report.issues.append(
                Issue(
                    type=IssueType.UNSUPPORTED_CLAIM,
                    severity=Severity.HIGH,
                    text=text[:200],
                    reason=finding.reason,
                    section=finding.section,
                )
            )

        for role_finding in roles:
            report.unsupported_claims += 1
            report.issues.append(
                Issue(
                    type=IssueType.CONTRADICTION,
                    severity=Severity.CRITICAL,
                    text=text[:200],
                    reason=role_finding.detail,
                    section=role_finding.section,
                )
            )

    # ── Позиция ответчика (ст. 12, ч. 3–4 ст. 198 ГПК РФ) ──────────
    # Возражения в деле есть, а в проекте нет ни одного довода — это
    # нарушение состязательности, по которому в реальном процессе
    # решение отменяют. Замечание ставится один раз на весь проект,
    # а не на раздел: обсуждать возражения можно в любом разделе.
    draft_text = "\n".join(s.text for s in draft.sections).lower()
    objection_texts = [i.text for i in pack.items if i.category == "objections"]
    if objection_texts:
        markers = (
            "довод", "возраж", "отклон", "не согласн", "оспор",
            "приняты во внимание", "подтверждаются материалами",
        )
        if not any(marker in draft_text for marker in markers):
            report.issues.append(
                Issue(
                    type=IssueType.OBJECTIONS_UNADDRESSED,
                    severity=Severity.HIGH,
                    text=objection_texts[0][:200],
                    reason=(
                        "в материалах дела есть возражения ответчика, "
                        "но проект не отвечает ни на один довод "
                        "(ст. 12, ч. 3–4 ст. 198 ГПК РФ)"
                    ),
                )
            )

    # ── Досудебный порядок (ст. 132, 135, 222 ГПК РФ) ──────────────
    # Договор или закон могут требовать претензионный порядок до суда,
    # и это условие рассмотрения иска. Если условие в материалах есть,
    # а проект молчит о соблюдении — судья не видит вопроса, который
    # надо разрешить до существа спора. Материалы без условия молчат
    # тоже: претензии нет ни в договоре, ни в иске — нечего проверять.
    pretrial_items = [
        item
        for item in pack.items
        if "досудебн" in item.text.lower() or "претензи" in item.text.lower()
    ]
    if pretrial_items and "досудебн" not in draft_text and "претензи" not in draft_text:
        report.issues.append(
            Issue(
                type=IssueType.PRETRIAL_UNADDRESSED,
                severity=Severity.HIGH,
                text=pretrial_items[0].text[:200],
                reason=(
                    "в материалах дела условие о досудебном "
                    "(претензионном) порядке урегулирования, проект не "
                    "отражает, соблюдён ли он (ст. 132, 135, 222 ГПК РФ)"
                ),
            )
        )

    # ── Госпошлина в цене иска (ч. 1 ст. 91 ГПК РФ) ────────────────
    # «Цена иска 150 000 руб., включая госпошлину»: пошлина — судебный
    # расход (ст. 98 ГПК РФ), а не часть цены иска. Завышенная цена
    # иска искажает расчёт самой пошлины по ст. 333.19 НК РФ, поэтому
    # ошибка касается и резолютивной части.
    fee_in_price = _FEE_IN_PRICE_RE.search(draft_text)
    if fee_in_price is None:
        for item in pack.by_category("claims"):
            fee_in_price = _FEE_IN_PRICE_RE.search(item.text)
            if fee_in_price:
                break
    if fee_in_price:
        matched = fee_in_price.string
        report.issues.append(
            Issue(
                type=IssueType.FEE_IN_CLAIM_PRICE,
                severity=Severity.MEDIUM,
                text=matched[
                    max(0, fee_in_price.start() - 60) : fee_in_price.start() + 80
                ],
                reason=(
                    "госпошлина указана включённой в цену иска: по ч. 1 "
                    "ст. 91 ГПК РФ цена иска определяется без госпошлины, "
                    "госпошлина — судебный расход (ст. 98 ГПК РФ)"
                ),
            )
        )

    # ── Заявления, меняющие исход (п. 76) ───────────────────────────
    # Пропуск срока исковой давности (ст. 199 ГК РФ) и снижение
    # неустойки (ст. 333 ГК РФ) суд разрешает только по заявлению
    # стороны — по своей инициативе они не применяются. Если такое
    # заявление есть в отзывах или протоколе, а проект о нём молчит,
    # судья не видит вопроса, который надо разрешить отдельно.
    # Молчание проекта проверяется по тем же маркерам, что и наличие
    # в материалах: лишнего не выдумываем.
    party_statements = [
        item
        for item in pack.items
        if item.category in ("objections", "protocol_findings", "disputed_facts")
    ]
    statement_text = " ".join(i.text.lower() for i in party_statements)

    def _find_statement(*needles: str) -> EvidenceItem | None:
        """Фрагмент-заявление, в котором нашлось хоть одно слово-маркер.

        Ищется по тому же признаку, по которому сработало условие, чтобы
        случай не StopIteration, когда маркер собран из разных фрагментов.
        """
        for item in party_statements:
            low = item.text.lower()
            if any(needle in low for needle in needles):
                return item
        # Ни один фрагмент не содержит маркер целиком (он собран из
        # разных фрагментов) — источником считаем «неизвестно», а не
        # первый фрагмент: цитата обязана указывать на заявление, иначе
        # судья увидит неверную ссылку (дефект D3).
        return None

    # Корень «давност» ловит все падежи («о пропуске срока исковой
    # давности», «давност»); точные фразы падежами разъезжались.
    if "давност" in statement_text and "давност" not in draft_text:
        source = _find_statement("давност")
        report.issues.append(
            Issue(
                type=IssueType.STATEMENT_LIMITATION,
                severity=Severity.HIGH,
                text=(source.text[:200] if source else ""),
                reason=(
                    "в материалах заявление о пропуске срока исковой "
                    "давности, а проект его не разрешает — суд применяет "
                    "давность только по заявлению стороны (ст. 199 ГК РФ, "
                    "ч. 2 ст. 196, п. 3 ст. 199 ГПК РФ)"
                ),
            )
        )

    # Снижение неустойки: корни «сниж»/«уменьш» плюс прямая ссылка на
    # ст. 333 ГК. Самого слова «неустойка» мало — ответчик может жаловаться
    # на размер долга, не прося снижения. «333» ищется как статья
    # («ст. 333»), а не как число: иначе сумма «333 рубля» давала ложное
    # замечание (дефект D4).
    reduction_requested = "неустойк" in statement_text and (
        "сниж" in statement_text
        or "уменьш" in statement_text
        or re.search(r"ст\.?\s*333", statement_text)
    )
    if reduction_requested and "неустойк" not in draft_text and "333" not in draft_text:
        source = _find_statement("сниж", "уменьш", "333")
        report.issues.append(
            Issue(
                type=IssueType.STATEMENT_PENALTY_REDUCTION,
                severity=Severity.HIGH,
                text=(source.text[:200] if source else ""),
                reason=(
                    "в материалах заявление о снижении неустойки, а проект "
                    "о нём не упоминает — суд снижает неустойку только по "
                    "заявлению стороны (ст. 333 ГК РФ)"
                ),
            )
        )

    # ── Таблица «обстоятельство — доказательства» ──────────────────
    # Считается по материалам, а не по проекту: она отвечает на вопрос,
    # откуда взято каждое обстоятельство, и её нельзя подделать текстом
    # модели.
    report.facts = subject_of_proof(pack.items)

    # ── Оценка доказательств (п. 5, ст. 67 ГПК РФ) ─────────────────
    # Суд оценивает каждое доказательство по относимости, допустимости
    # и достоверности, а все в совокупности — по достаточности. Система
    # оценку не выносит: она показывает, какие критерии нашлись в
    # проекте, а какие нет, и выносит вопросы наружу — позицию стороны,
    # выданную за доказательство, и заключение эксперта, принятое без
    # оценки наравне с другими (ч. 3 ст. 86 ГПК РФ).
    report.assessment = assessment_of(draft_text)

    assessment_issues = {
        "pleading_as_evidence": (IssueType.PLEADING_AS_EVIDENCE, Severity.HIGH),
        "expert_unassessed": (IssueType.EXPERT_UNASSESSED, Severity.MEDIUM),
        "not_assessed": (IssueType.EVIDENCE_NOT_ASSESSED, Severity.MEDIUM),
    }
    for assessment_finding in check_evidence_assessment(
        draft_text, [item.text for item in pack.items]
    ):
        issue_type, severity = assessment_issues[assessment_finding.kind]
        report.issues.append(
            Issue(
                type=issue_type,
                severity=severity,
                text=assessment_finding.evidence,
                reason=assessment_finding.reason,
            )
        )

    # ── Итоговый статус ────────────────────────────────────────────
    critical = [i for i in report.issues if i.severity is Severity.CRITICAL]
    if critical or report.issues:
        report.status = VerificationStatus.REVIEW_REQUIRED

    return report


def compare_versions(text_a: str, text_b: str) -> dict[str, int]:
    """Сравнить две версии проекта (п.31).

    Простой подсчёт различий по предложениям: полноценный diff нужен
    только для отображения правок, а не для хранения — хранятся полные
    тексты версий.
    """
    sentences_a = [s.strip() for s in re.split(r"(?<=[.;])\s+", text_a) if s.strip()]
    sentences_b = [s.strip() for s in re.split(r"(?<=[.;])\s+", text_b) if s.strip()]
    set_a, set_b = set(sentences_a), set(sentences_b)
    return {
        "sentences_before": len(sentences_a),
        "sentences_after": len(sentences_b),
        "removed": len(set_a - set_b),
        "added": len(set_b - set_a),
        "unchanged": len(set_a & set_b),
    }
