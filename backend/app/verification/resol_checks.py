"""Проверки резолютивной части: только текст решения и метаданные дела.

Набор задан датасетом tests/data/resol_fixtures.json: каждое правило имеет хотя бы
одну фикстуру с меткой defect и ни одного ложного срабатывания на clean.

Правило без нужных метаданных молчит, а не угадывает: отсутствие данных в тексте
не равно отсутствию факта в деле.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.verification.laws import state_fee

# Разбивка только перед заглавной буквой: «…2 000,00 руб. и расходов на …» — одно предложение.
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[«А-ЯЁ])")
_MONEY = re.compile(r"(\d[\d\s]*[.,]\d{2})\s*руб")
_FEE_TOTAL = re.compile(r"всего подлежит взысканию\s+(\d[\d\s]*[.,]\d{2})", re.I)
_VYSK = re.compile(r"^взыскать\b", re.I)


@dataclass(frozen=True)
class Defect:
    """Одно срабатывание проверки в резолютивной части."""

    id: str
    severity: str
    fragment: str
    reason: str


def sentences(text: str) -> list[str]:
    """Разбивает резолютивную часть на предложения."""
    return [s.strip() for s in _SPLIT.split(text) if s.strip()]


def _amount(value: str) -> float:
    return float(value.replace(" ", "").replace("\u00a0", "").replace(",", "."))


def _no_costs(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 1: распределение госпошлины и судебных издержек отсутствует."""
    if re.search(r"пошлин|судебн\w*\s*расход|издержк", text, re.I):
        return []
    return [
        Defect(
            "RESOL_NO_COSTS",
            "HIGH",
            "",
            "нет пункта о распределении государственной пошлины и судебных издержек",
        )
    ]


# Связки, которыми пошлина и иные издержки свалены в один пункт.
_MERGE_LINK = re.compile(
    r"состоящие из|а всего|в том числе|и расход\w* на|и судебн\w* издержк",
    re.I,
)


def _costs_merged(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 2: госпошлина и иные издержки в одном пункте, а не раздельно."""
    out: list[Defect] = []
    for s in sentences(text):
        if not _VYSK.search(s):
            continue
        if not re.search(r"пошлин", s, re.I):
            continue
        if not re.search(r"представител|экспертиз|издержк", s, re.I):
            continue
        if _MERGE_LINK.search(s):
            out.append(
                Defect(
                    "RESOL_COSTS_MERGED",
                    "MEDIUM",
                    s[:120],
                    "госпошлина и иные издержки объединены в одном пункте",
                )
            )
    return out


def _total_mismatch(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 3: строка «Всего подлежит взысканию» не равна сумме пунктов."""
    m = _FEE_TOTAL.search(text)
    if not m:
        return []
    total = _amount(m.group(1))
    parts = [s for s in sentences(text) if _VYSK.search(s)]
    computed = sum(_amount(a) for s in parts for a in _MONEY.findall(s))
    if abs(computed - total) <= 0.011:
        return []
    return [
        Defect(
            "RESOL_TOTAL_MISMATCH",
            "MEDIUM",
            m.group(0),
            f"сумма пунктов {computed:.2f} не равна заявленным {total:.2f}",
        )
    ]


def _no_amount(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 4: имущественное требование без определённой суммы."""
    out: list[Defect] = []
    for s in sentences(text):
        if not re.search(r"в размере", s, re.I):
            continue
        if not re.search(r"подлежащем определению|подлежит установлению", s, re.I):
            continue
        if _MONEY.search(s):
            continue
        out.append(Defect("RESOL_NO_AMOUNT", "HIGH", s[:120], "размер требования не определён"))
    return out


def _fully_partially(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 5: «удовлетворить полностью» вместе с отказом в остальной части."""
    if not re.search(r"удовлетворить полностью", text, re.I):
        return []
    if not re.search(r"в остальной части", text, re.I) or not re.search(r"отказать", text, re.I):
        return []
    return [
        Defect(
            "RESOL_FULLY_PARTIALLY",
            "HIGH",
            "",
            "полное удовлетворение совмещено с отказом в остальной части",
        )
    ]


Rule = Callable[[str, Mapping[str, Any]], list[Defect]]

# ── Пачка 2: стороны ───────────────────────────────────────────────────────
_PARTY_ORG = r"(?:ООО|АО|ПАО|ЗАО|ГУП|МУП)\s+«[^»]+»"
_PARTY_IP = r"ИП\s+[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){0,2}"
_PARTY_NAME = r"[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2}"
_PARTY = rf"(?:{_PARTY_ORG}|{_PARTY_IP}|{_PARTY_NAME})"
_RECIPIENT = re.compile(rf"в пользу\s+({_PARTY})")
_DEBTOR = re.compile(rf"взыскать\s+с\s+({_PARTY})", re.I)
_ID = re.compile(r"ИНН|СНИЛС|паспорт|водительск|ОГРН", re.I)
_SIDE_STOP = re.compile(r"\s+в\s+|\s+и\s+|\s+доход\b|[,.;]")
_COSTS = re.compile(r"пошлин|расход|издержк|бюджет", re.I)


def _window(s: str, start: int) -> str:
    """Текст между концом имени стороны и ближайшим концом этой конструкции."""
    m = _SIDE_STOP.search(s, start)
    return s[start : m.start()] if m else s[start:]


def _no_whom(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 6: взыскание без указания, с кого и в чью пользу."""
    out: list[Defect] = []
    for s in sentences(text):
        if not _VYSK.search(s) or _COSTS.search(s):
            continue
        if _RECIPIENT.search(s) or re.search(r"в\s+доход\b|\bс\s+[«А-ЯЁ]", s):
            continue
        out.append(
            Defect("RESOL_NO_WHOM", "HIGH", s[:120], "не указано, с кого и в чью пользу взыскание")
        )
    return out


def _no_id(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 9: у стороны рядом с именем нет идентификатора.

    Лица с «года рождения» сторонами не считаются: в тексте они встречаются
    не после «с» и не после «в пользу».
    """
    out: list[Defect] = []
    for s in sentences(text):
        for pattern in (_DEBTOR, _RECIPIENT):
            for m in pattern.finditer(s):
                party = m.group(1)
                if _ID.search(_window(s, m.end())):
                    continue
                out.append(
                    Defect(
                        "RESOL_NO_ID",
                        "MEDIUM",
                        party,
                        "у стороны рядом с именем нет ИНН/СНИЛС/паспорта",
                    )
                )
    return out


def _no_shares(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 10: несколько ответчиков в одном взыскании без долей и без солидарности."""
    out: list[Defect] = []
    for s in sentences(text):
        if not _VYSK.search(s) or re.search(r"солидарн", s, re.I):
            continue
        m = _DEBTOR.search(s)
        if not m:
            continue
        seg = s[m.start() :]
        seg = re.split(r" в пользу | в доход ", seg, maxsplit=1)[0]
        # «и» внутри названия (ООО «Кузьмин и партнёры») соединением сторон не является.
        seg = re.sub(r"«[^»]*»", "", seg)
        if re.search(r"\sи\s+[«А-ЯЁ]", seg) and not re.search(r"дол|1/\d", seg):
            out.append(
                Defect(
                    "RESOL_NO_SHARES",
                    "HIGH",
                    seg[:120],
                    "несколько ответчиков без солидарной ответственности или долей",
                )
            )
    return out


# ── Пачка 3: форма и сроки ────────────────────────────────────────────────


def _no_appeal(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 7: в резолютивной части нет порядка и срока обжалования."""
    if re.search(r"обжалован", text, re.I):
        return []
    return [Defect("RESOL_NO_APPEAL", "HIGH", "", "не указан порядок и срок обжалования")]


def _no_motivated(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 8: указан мировой судья, но нет блока 3/15 дней (ч. 4 ст. 199 ГПК)."""
    if not re.search(r"миров\w*\s+судь", text, re.I):
        return []
    have_all = (
        re.search(r"мотивированн", text, re.I)
        and re.search(r"тр[её]х\s+дней", text, re.I)
        and re.search(r"пятнадцати", text, re.I)
    )
    if have_all:
        return []
    return [
        Defect(
            "RESOL_NO_MOTIVATED",
            "MEDIUM",
            "",
            "нет блока о подаче заявления о мотивированном решении (3/15 дней)",
        )
    ]


def _no_immediate(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 11: решения по ст. 211 ГПК без фразы о немедленном исполнении."""
    if re.search(r"немедленному исполнению", text, re.I):
        return []
    for s in sentences(text):
        is_alimony = re.search(r"алимент", s, re.I)
        is_wage = bool(
            re.search(r"заработн\w*\s+плат", s, re.I)
            and re.search(r"за\s+три\s+месяца|тр[её]х\s+месяцев", s, re.I)
        )
        is_reinstate = bool(
            re.search(r"восстанов", s, re.I) and re.search(r"на\s+работ", s, re.I)
        )
        if is_alimony or is_wage or is_reinstate:
            return [
                Defect(
                    "RESOL_NO_IMMEDIATE",
                    "HIGH",
                    s[:120],
                    "решение по ст. 211 ГПК без указания на немедленное исполнение",
                )
            ]
    return []


def _no_deadline(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 13: обязательство без срока (дата в договоре сроком не считается)."""
    out: list[Defect] = []
    for s in sentences(text):
        if not re.search(r"обязать", s, re.I):
            continue
        has_term = re.search(
            r"немедленн|в\s+течение[^.!?]*(?:дн\w*|дес\w*|мес\w*|год)|до\s+\d{2}\.\d{2}\.\d{4}",
            s,
            re.I,
        )
        if not has_term:
            out.append(Defect("RESOL_NO_DEADLINE", "MEDIUM", s[:120], "не указан срок исполнения"))
    return out


def _no_property_value(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 12: обязать передать/изъять имущество без указания его стоимости."""
    out: list[Defect] = []
    for s in sentences(text):
        if not re.search(r"обязать", s, re.I) or not re.search(r"передать|изъять", s, re.I):
            continue
        if not re.search(r"стоимост", s, re.I):
            out.append(
                Defect(
                    "RESOL_NO_PROPERTY_VALUE",
                    "MEDIUM",
                    s[:120],
                    "не указана стоимость передаваемого (изымаемого) имущества",
                )
            )
    return out


def _zaochno_no_otmena(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 19: заочное решение без блока об отмене (7 дней со дня вручения)."""
    if not meta.get("default_judgment"):
        return []
    for s in sentences(text):
        if not re.search(r"отмен", s, re.I):
            continue
        if re.search(r"семь|семи|\b7\b", s, re.I) and re.search(r"вручен", s, re.I):
            return []
    return [
        Defect(
            "RESOL_ZAOCHNO_NO_OTMENA_ORDER",
            "HIGH",
            "",
            "нет блока о праве ответчика на отмену заочного решения в семидневный срок",
        )
    ]


def _alimony_indexation(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 20: алименты в твёрдой сумме без порядка индексации (п. 1 ст. 117 СК)."""
    if not re.search(r"алимент", text, re.I) or not re.search(r"тв[её]рд", text, re.I):
        return []
    if re.search(r"индексац", text, re.I):
        return []
    return [
        Defect(
            "RESOL_ALIMONY_NO_INDEXATION",
            "HIGH",
            "",
            "алименты в твёрдой сумме без порядка их индексации",
        )
    ]


def _alimony_period(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 21: не указан период алиментов — начиная с чего и до какого срока."""
    if not re.search(r"алимент", text, re.I):
        return []
    start = re.search(r"начиная\s+с|с\s+момента\s+обращения|с\s+момента\s+подачи", text, re.I)
    end = re.search(r"до\s+совершеннолетия|до\s+достижения", text, re.I)
    if start and end:
        return []
    return [
        Defect(
            "RESOL_ALIMONY_NO_PERIOD",
            "HIGH",
            "",
            "не указан период: начиная с какого момента и до какого срока",
        )
    ]


# ── Пачка 4: пропорция, льготы, мотивировка ───────────────────────────────
_MOTIVATION_MARKERS = re.compile(
    r"поскольку|так\s+как|в\s+связи\s+с\s+тем,\s+что|исходя\s+из",
    re.I,
)
# В должнике бывает указан идентификатор — «(СНИЛС: …)», его надо отбросить.
_FEE_ID = re.compile(r"\s*\([^)]*\)\s*$")
_FEE_FROM = re.compile(
    r"взыскать\s+с\s+(.*?)(?:\s+в\s+доход|\s+в\s+пользу|\s+государственную\s+пошлину|\s+расход)",
    re.I,
)


def _no_consumer_fine(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 14: потребительский спор без штрафа (п. 6 ст. 13 ЗоЗПП)."""
    if not meta.get("consumer_case"):
        return []
    if not re.search(r"товар|услуг", text, re.I) or "в пользу" not in text:
        return []
    if re.search(r"штраф", text, re.I):
        return []
    return [
        Defect(
            "RESOL_NO_CONSUMER_FINE",
            "HIGH",
            "",
            "по делу о правах потребителя не взыскан штраф по п. 6 ст. 13 ЗоЗПП",
        )
    ]


def _proportion_non_property(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 15: пропорциональное деление взято по неимущественным требованиям."""
    if not re.search(r"пропорциональн", text, re.I):
        return []
    if not re.search(r"моральн", text, re.I) or not re.search(r"вред", text, re.I):
        return []
    return [
        Defect(
            "RESOL_PROPORTION_NON_PROPERTY",
            "HIGH",
            "",
            "пропорциональное деление применено к моральному вреду (неимущественному требованию)",
        )
    ]


def _proportion_reduced(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 16: пропорциональность взята по мотиву снижения по ст. 333 ГК РФ."""
    if not re.search(r"пропорциональн", text, re.I):
        return []
    if not re.search(r"ст\.\s*333|333\s+ГК", text, re.I):
        return []
    return [
        Defect(
            "RESOL_PROPORTION_REDUCED",
            "HIGH",
            "",
            "пропорция отнесена к снижению неустойки по ст. 333 ГК РФ",
        )
    ]


def _fee_from_exempt(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 17: пошлина взыскана с льготного истца (ст. 333.36 НК РФ)."""
    if not meta.get("claimant_exempt"):
        return []
    recipients = {m.group(1).strip() for m in _RECIPIENT.finditer(text)}
    out: list[Defect] = []
    for s in sentences(text):
        if not _VYSK.search(s) or not re.search(r"пошлин", s, re.I):
            continue
        m = _FEE_FROM.search(s)
        debtor = _FEE_ID.sub("", m.group(1)).strip() if m else ""
        if debtor and debtor in recipients:
            out.append(
                Defect(
                    "RESOL_FEE_FROM_EXEMPT",
                    "HIGH",
                    s[:120],
                    "госпошлина взыскана с истца, имеющего льготу по ст. 333.36 НК РФ",
                )
            )
    return out


def _motivation_in_operative(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 18: в резолютивную часть перенесена мотивировка (ч. 4 ст. 198 ГПК)."""
    out: list[Defect] = []
    for s in sentences(text):
        if not _VYSK.search(s):
            continue
        if _MOTIVATION_MARKERS.search(s):
            out.append(
                Defect(
                    "RESOL_MOTIVATION_IN_OPERATIVE",
                    "MEDIUM",
                    s[:120],
                    "мотивировка в резолютивной части",
                )
            )
    return out


# ── Пачка 5: пошлина по материалам дела ───────────────────────────────────
#: Допуск в рубль — тот же, что у сверки пошлины в engine.py.
_FEE_TOLERANCE = 1.0
#: Даты-представители редакций: определяют, чьей сеткой уплачена пошлина.
_FEE_EDITIONS: tuple[tuple[str, date], ...] = (
    ("редакцией до 29.01.2010", date(2005, 1, 1)),
    ("редакцией от 29.01.2010 до 08.09.2024", date(2020, 1, 1)),
    ("редакцией с 09.09.2024", date(2025, 1, 1)),
)


def _wrong_fee_edition(text: str, meta: Mapping[str, Any]) -> list[Defect]:
    """Проверки 22: пошлина уплачена по редакции ст. 333.19, не действовавшей на дату подачи.

    Метаданные обязательны: без даты подачи, цены иска и уплаченной суммы
    проверка молчит. Расхождение, не сходящееся ни с одной редакцией, — это
    арифметика, а не неверная редакция, и оно тоже молчит.
    """
    claim_date = meta.get("claim_date")
    claim_price = meta.get("claim_price")
    paid = meta.get("paid_fee")
    if not claim_date or claim_price is None or paid is None:
        return []
    try:
        on = date.fromisoformat(str(claim_date)[:10])
        price = float(claim_price)
        paid_fee = float(paid)
    except (TypeError, ValueError):
        return []
    expected = state_fee(price, on)
    if abs(expected - paid_fee) <= _FEE_TOLERANCE:
        return []
    for label, moment in _FEE_EDITIONS:
        if abs(state_fee(price, moment) - paid_fee) <= _FEE_TOLERANCE:
            return [
                Defect(
                    "RESOL_WRONG_FEE_EDITION",
                    "HIGH",
                    f"{paid_fee:.2f} руб. при цене иска {price:.2f} руб. на {on.isoformat()}",
                    f"сумма сходится с {label}, но на дату подачи действовала другая "
                    f"(расчёт {expected:.2f} руб.)",
                )
            ]
    return []


RULES: list[Rule] = [
    _no_costs,
    _costs_merged,
    _total_mismatch,
    _no_amount,
    _fully_partially,
    _no_whom,
    _no_id,
    _no_shares,
    _no_appeal,
    _no_motivated,
    _no_immediate,
    _no_deadline,
    _no_property_value,
    _zaochno_no_otmena,
    _alimony_indexation,
    _alimony_period,
    _no_consumer_fine,
    _proportion_non_property,
    _proportion_reduced,
    _fee_from_exempt,
    _motivation_in_operative,
    _wrong_fee_edition,
]


def check_resolution(text: str, meta: Mapping[str, Any] | None = None) -> list[Defect]:
    """Возвращает все срабатывания проверок для текста резолютивной части."""
    data: Mapping[str, Any] = meta or {}
    out: list[Defect] = []
    for rule in RULES:
        out.extend(rule(text, data))
    return out
