"""Вопросы о квалификации договора и подведомственности (п. 3–4 разбора).

Система не решает за судью, принадлежит ли спор арбитражу и как
квалифицировать договор. Но она обязана вынести вопрос наружу, а не
пропустить его молча. Рецензент поймал два случая, когда этого не было:

* договор поставки, а истец указан физическим лицом. Поставщиком по
  ст. 506 ГК РФ может быть предприниматель, поэтому без признака ИП
  отношения могут оказаться куплей-продажей (ст. 454, 486 ГК РФ);
* истец — ИП, ответчик — организация. Тогда спор из договора поставки
  подсуден арбитражному суду (ст. 27, 28 АПК РФ), и суд общей
  юрисдикции должен передать дело (ч. 2.1 ст. 33 ГПК РФ).

Обе проверки ищут признаки в текстах материалов и в участниках дела.
Нет признака — проверка молчит: отсутствие слова не повод задавать
вопрос. Сами выводы не делаются — формулируется вопрос судье.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Договор поставки. «Поставщик» и «поставки» рядом с договором —
#: тот же признак, что и слово «поставка» в заголовке.
_SUPPLY_RE = re.compile(r"поставк|поставщик|поставки", re.IGNORECASE)

#: Признак ИП. Отдельно «ИП» как самостоятельное слово: подстрока «ип»
#: встречается в «заявитель», «писание» и ломала бы проверку.
_IP_RE = re.compile(
    r"индивидуальн\w*\s+предпринимател\w*|\bИП\b|\bИ\.П\.",
    re.IGNORECASE,
)

#: ООО, ЗАО, АО и прочие организационные формы ответчика.
_ORG_RE = re.compile(
    r"\bООО\b|\bЗАО\b|\bОАО\b|\bАО\b|\bПАО\b|компани|корпорац",
    re.IGNORECASE,
)

#: Физическое лицо прямо названо таковым.
_NATURAL_RE = re.compile(r"физическ\w*\s+лиц\w*|гражданин|гражданк", re.IGNORECASE)

#: Фамилия Имя Отчество подряд — признак физлица в участниках дела.
_FULL_NAME_RE = re.compile(
    r"[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})?"
)


@dataclass(frozen=True)
class QualificationFinding:
    """Вопрос, который судья должен разрешить до существа спора."""

    kind: str  # "qualification" | "arbitration"
    reason: str  # готовый текст вопроса


def has_supply(texts: list[str]) -> bool:
    """Есть ли в материалах дела признак договора поставки."""
    return any(_SUPPLY_RE.search(text) for text in texts)


def _plaintiff_is_natural(plaintiff: str, texts: list[str]) -> bool:
    """Истец — физическое лицо без признака ИП.

    Проверяется и по самому имени участника (ФИО), и по текстам:
    «физическое лицо» в иске надёжнее, чем форма имени.
    """
    if not plaintiff:
        return False
    if _IP_RE.search(plaintiff):
        return False
    if _ORG_RE.search(plaintiff):
        return False
    return bool(_FULL_NAME_RE.match(plaintiff.strip())) or any(
        _NATURAL_RE.search(text) for text in texts
    )


def check_qualification(
    texts: list[str], plaintiff: str = "", defendant: str = ""
) -> list[QualificationFinding]:
    """Сформулировать вопросы о квалификации и подведомственности.

    Возвращает пусто, когда по материалам не из чего строить вопрос:
    спор не о поставке, или истец — организация, или ИП подтверждён.
    """
    if not has_supply(texts):
        return []

    findings: list[QualificationFinding] = []
    ip_marked = any(_IP_RE.search(text) for text in texts) or _IP_RE.search(
        plaintiff or ""
    )
    defendant_is_org = bool(defendant and _ORG_RE.search(defendant)) or any(
        _ORG_RE.search(text) for text in texts
    )
    # ИП-ИП — тоже арбитраж: ст. 27 АПК РФ относит к нему споры
    # между индивидуальными предпринимателями, связанные с
    # предпринимательской деятельностью, и спор о поставке между ИП —
    # из таких. Раньше требовалась организация-ответчик, и дело двух ИП
    # оставалось «общей юрисдикции».
    defendant_is_ip = bool(defendant and _IP_RE.search(defendant))

    if not ip_marked and _plaintiff_is_natural(plaintiff, texts):
        findings.append(
            QualificationFinding(
                kind="qualification",
                reason=(
                    "договор поставки, но истец указан физическим лицом без "
                    "признака индивидуального предпринимателя: уточнить, "
                    "является ли истец ИП; если нет, отношения могут "
                    "квалифицироваться как купля-продажа (ст. 506, 454, "
                    "486 ГК РФ)"
                ),
            )
        )

    if ip_marked and (defendant_is_org or defendant_is_ip):
        counterparty = (
            "ответчик — ИП" if defendant_is_ip and not defendant_is_org
            else "ответчик — организация"
        )
        findings.append(
            QualificationFinding(
                kind="arbitration",
                reason=(
                    f"истец — ИП, {counterparty}, спор из договора "
                    "поставки: подведомственность арбитражного суда "
                    "(ст. 27, 28 АПК РФ); при общей юрисдикции дело "
                    "подлежит передаче по подсудности (ч. 2.1 ст. 33 ГПК "
                    "РФ)"
                ),
            )
        )

    return findings
