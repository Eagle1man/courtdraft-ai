"""Вид судопроизводства — из материалов дела, а не из поля формы.

Зачем: разбор 26 проектов после живого теста показал, что 21 из них
применял ГПК РФ к арбитражным делам. Причина не в модели: интерфейс
всегда отправлял `case_type = "civil"`, а генератор всегда брал шаблон
`civil_judgment`. Модель получала гражданский шаблон и добросовестно
применяла ГПК.

Правильное место для вида судопроизводства — сами материалы: номер дела
вида «А40-…», шапка «Арбитражный суд», ссылки на АПК РФ. Здесь эти
признаки ищутся простыми правилами, без модели: ошибка дешевле пропуска.
Не нашли уверенно — оставляем прежнее значение, а не угадываем.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: Значения поля `Case.case_type`, которые понимает генератор.
ARBITRATION = "arbitration"
CIVIL = "civil"

#: Уверенность ниже этой — тип не меняется.
_MIN_LEAD = 4
_MIN_MARGIN = 3
_HIGH_CONFIDENCE = 8


@dataclass(frozen=True)
class JurisdictionVerdict:
    """Что удалось понять о виде судопроизводства."""

    case_type: str = ""
    confidence: str = ""  # "high" | "medium" | ""
    reasons: tuple[str, ...] = ()

    @property
    def detected(self) -> bool:
        """Есть ли уверенный ответ, по которому можно выбирать шаблон."""
        return bool(self.case_type)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_type": self.case_type,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
        }


#: Признаки арбитража: (шаблон, вес, объяснение).
_ARBITRATION_MARKERS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (
        re.compile(r"арбитражн\w*\s+суд", re.IGNORECASE),
        4,
        "в материалах назван арбитражный суд",
    ),
    (
        re.compile(
            r"\bАПК\s*РФ\b|арбитражн\w+\s+процессуальн\w+\s+кодекс",
            re.IGNORECASE,
        ),
        4,
        "в материалах ссылка на АПК РФ",
    ),
    (
        # Номер арбитражного дела: А40-12345/2026, А56-1/2025.
        re.compile(r"\bА\d{1,2}-\d{1,7}/\d{4}\b"),
        4,
        "номер дела арбитражного суда вида А40-…",
    ),
    (
        re.compile(r"\bИНН\b|\bОГРН\b", re.IGNORECASE),
        1,
        "ИНН/ОГРН — реквизиты организаций",
    ),
)

#: Признаки суда общей юрисдикции.
_CIVIL_MARKERS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (
        re.compile(r"миров\w+\s+судья|судебн\w+\s+участок", re.IGNORECASE),
        4,
        "в материалах мировой судья",
    ),
    (
        re.compile(
            r"районн\w*\s+суд|городск\w*\s+суд|областн\w*\s+суд|"
            r"краев\w*\s+суд|республиканск\w*\s+суд",
            re.IGNORECASE,
        ),
        4,
        "в материалах суд общей юрисдикции",
    ),
    (
        re.compile(
            r"\bГПК\s*РФ\b|гражданск\w+\s+процессуальн\w+\s+кодекс",
            re.IGNORECASE,
        ),
        4,
        "в материалах ссылка на ГПК РФ",
    ),
    (
        re.compile(r"потребител", re.IGNORECASE),
        1,
        "спор с участием потребителя",
    ),
)

_IP_RE = re.compile(
    r"индивидуальн\w*\s+предпринимател\w*|\bИП\b|\bИ\.П\.",
    re.IGNORECASE,
)
_ORG_RE = re.compile(
    r"\bООО\b|\bЗАО\b|\bОАО\b|\bАО\b|\bПАО\b|компани|корпорац",
    re.IGNORECASE,
)


def _score(
    texts: Iterable[str],
    markers: tuple[tuple[re.Pattern[str], int, str], ...],
) -> tuple[int, list[str]]:
    """Сумма весов найденных признаков и их объяснения.

    Признак учитывается один раз: десять упоминаний «арбитражный суд» в
    одном документе — это по-прежнему один признак, а не десять.
    """
    joined = "\n".join(text or "" for text in texts)
    total = 0
    reasons: list[str] = []
    for pattern, weight, reason in markers:
        if pattern.search(joined):
            total += weight
            reasons.append(reason)
    return total, reasons


def _sides_hint(plaintiff: str, defendant: str) -> tuple[int, int, list[str]]:
    """Признаки вида судопроизводства по составу сторон.

    Спор между организациями и ИП — арбитраж (ст. 27 АПК РФ). Истец —
    физическое лицо без признака ИП — обычно общая юрисдикция. Подсказка
    слабая: только дополняет текстовые признаки, сама тип не выбирает.
    """
    arb = 0
    civil = 0
    reasons: list[str] = []

    def is_business(value: str) -> bool:
        return bool(value and (_ORG_RE.search(value) or _IP_RE.search(value)))

    if is_business(plaintiff) and is_business(defendant):
        arb += 3
        reasons.append("обе стороны — организации или ИП")
    elif plaintiff and not is_business(plaintiff) and not _ORG_RE.search(plaintiff):
        civil += 1
        reasons.append("истец — физическое лицо")
    return arb, civil, reasons


def detect_jurisdiction(
    texts: Iterable[str],
    *,
    plaintiff: str = "",
    defendant: str = "",
) -> JurisdictionVerdict:
    """Определить вид судопроизводства по текстам материалов.

    Возвращает пустой вердикт, когда признаки слабые или противоречат
    друг другу: лучше оставить прежний тип, чем угадать и перепутать
    кодекс в проекте решения.
    """
    material = list(texts)
    arb, arb_reasons = _score(material, _ARBITRATION_MARKERS)
    civil, civil_reasons = _score(material, _CIVIL_MARKERS)
    side_arb, side_civil, side_reasons = _sides_hint(plaintiff, defendant)
    arb += side_arb
    civil += side_civil

    if abs(arb - civil) < _MIN_MARGIN:
        return JurisdictionVerdict()
    if max(arb, civil) < _MIN_LEAD:
        return JurisdictionVerdict()

    if arb > civil:
        reasons = tuple(arb_reasons + side_reasons)
        return JurisdictionVerdict(
            case_type=ARBITRATION,
            confidence="high" if arb >= _HIGH_CONFIDENCE else "medium",
            reasons=reasons,
        )
    reasons = tuple(civil_reasons + side_reasons)
    return JurisdictionVerdict(
        case_type=CIVIL,
        confidence="high" if civil >= _HIGH_CONFIDENCE else "medium",
        reasons=reasons,
    )
