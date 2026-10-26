"""Извлечение требований истца из пакета доказательств.

Зачем этот модуль. Резолютивная часть собирается по правилам
(app.drafting.operative), но правилам нужны требования в структурированном
виде. Брать их из текста модели нельзя — тогда весь смысл замены
раздела на детерминированную сборку теряется: модель перевратит
название требования, и ошибка вернётся.

Поэтому требования извлекаются регулярными выражениями из пакета
доказательств, где лежат фразы из материалов дела вместе с указанием
документа-источника. Это тот же принцип, что и с суммами: числа берутся
из материалов, а не выдумываются моделью.

ГЛАВНОЕ ОГРАНИЧЕНИЕ
    Модуль НЕ решает, удовлетворять требование или отказать: это решение
    принимает судья. Но когда исход уже есть в материалах дела — в
    установительной части («исков подлежит удовлетворению частично»,
    «взыскивает моральный вред в сумме 2 000 рублей») — проект обязан
    его отразить, а не выдавать отказ за полное удовлетворение. Такой
    исход подставляется в поле outcome (см. resolve_outcomes); если в
    материалах исхода нет, остаётся требование истца, и судья меняет
    его в интерфейсе.

ЧТО НЕ РАБОТАЕТ И ДОЛЖНО БЫТЬ ЗАМЕЧЕНО
    Если требование сформулировано словами, не подпадающими под
    шаблоны ниже, оно не попадёт в резолютивную часть. Модуль это
    не скрывает: такие находки возвращаются отдельным списком, и
    раздел помечается как требующий проверки. Лучше пустой раздел с
    пометкой, чем резолюция по догадке.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import NamedTuple

from app.agents.claim_phrases import (
    CLAIM_BASIS_ANCHORS,
    CLAIM_VERB_RE,
    NON_MONEY_BASES,
    claim_basis,
    claim_basis_match,
    looks_like_claim,
    payer_is_plaintiff,
)
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.drafting.operative import (
    Claimant,
    CounterClaim,
    MoneyClaim,
    Outcome,
    Parties,
    Procedure,
)

# Формулировки требований и их основания описаны в app.agents.claim_phrases
# и НЕ дублируются здесь: категория «claims» в пакете и извлечение
# требований должны видеть одно и то же, иначе резолютивная часть
# окажется пустой при заполненном пакете.

# Госпошлина: сумма идёт отдельным пунктом по ст. 98 ГПК РФ.
#
# Разряды разделяются пробелом или неразрывным пробелом, поэтому
# «5 100» и «5 100» — одно и то же число. Требование «четыре цифры
# подряд» было неверно: пошлина в 5 100 руб. — обычная величина, и
# без пробела она в демо не находилась вовсе.
_DIGITS = r"[\d\u00a0\u2009\u202f ]{2,}"

# Сумма прописью в скобках между цифрами и словом «руб»: в иске
# пошлина пишется как «4 000 (четыре тысячи) руб.». Без этой скобки
# шаблон не доходил до «руб» и пошлина в 4 000 руб. не находилась
# вовсе — пункт о распределении расходов в проект не попадал.
_FEE_WORDS = r"(?:\s*\([^)]{0,120}\))?"

FEE_PATTERNS = [
    re.compile(
        r"(?:госпошлин\w*|пошлин\w*)" + r"[^.;]{0,30}?" + r"(" + _DIGITS + r")"
        + _FEE_WORDS + r"\s*(?:руб(?:\.|ля|ль|лей|\b)|₽)",
        re.I,
    ),
    re.compile(
        r"(" + _DIGITS + r")" + _FEE_WORDS + r"\s*(?:руб(?:\.|ля|ль|лей|\b)?|₽)\s*"
        r"(?:в\s+том\s+числе\s+)?(?:госпошлин\w*|пошлин\w*)",
        re.I,
    ),
]

# Иные расходы по ст. 100 ГПК РФ.
COSTS_PATTERN = re.compile(
    r"(?:иные\s+расход\w*|расход\w*\s+на\s+(?:поездк|почтов\w+|\S+_ expert)"
    r"|экспертиз\w*\s+и|расход\w*\s+на\s+оплат\w*\s+экспертиз\w*)"
    r"|(?:затрат\w*|расход\w*)\s+на\s+проведение\s+экспертиз\w*",
    re.I,
)

_AMOUNT = re.compile(r"\d[\d\s]*")


def _to_number(value: str) -> int | None:
    """«150 000» -> 150000. Нераспознанное -> None, а не ноль."""
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:  # pragma: no cover — защита от неожиданного формата
        return None


def _find_amount_with_kopecks(text: str) -> tuple[int, int] | None:
    """Сумма вместе с копейками: (5034, 79).

    Копейки ищутся первыми, потому что в текстах решений сумма
    записывается как «5034 руб. 79 коп.», и раздельный поиск без
    копеек терял бы вторую часть — а именно она определяет итог
    взыскания, который предъявляют к исполнению.
    """
    with_kop = re.search(
        r"([\d][\d\s\u00a0\u2009\u202f]{2,})\s*(?:\((?:[^()\d]{3,120})\)\s*)?руб(?:лей|ля|ль|\.)?\s*"
        r"(\d{1,2})\s*коп",
        text,
        re.I,
    )
    if with_kop:
        whole = _to_number(with_kop.group(1))
        if whole is not None:
            return whole, int(with_kop.group(2))

    # Сумма, записанная только копейками: real-01 суд взыскал ЖКУ
    # «72 копейки», и без этой формы требование оставалось «в полном
    # объёме» на 11 107,84 — ложь в ту же сторону, что и отказ.
    kop_only = re.search(r"(?<![\d.,])(\d{1,2})\s*коп", text, re.I)
    if kop_only:
        return 0, int(kop_only.group(1))

    plain = _find_amount(text)
    return (plain, 0) if plain is not None else None


def _find_amount(text: str) -> int | None:
    """Найти сумму рядом с требованием.

    Отсев нужен по двум причинам, и обе проверены тестами на живых
    прогонах:

    - год («за 2019 год») не является суммой взыскания;
    - номер дела и код участника содержат цифры, которые выглядят
      как сумма, но означают другое.

    Поэтому суммой считается только число, при котором в тексте
    стоит признак денег — «руб», «₽», «тыс. руб.». Без такого
    признака сумма не подтверждена и остаётся незаполненной: лучше
    пробел, заполняемый судьёй, чем неверная цифра в проекте решения.
    """
    with_currency = re.search(
        r"([\d][\d\s]{2,})\s*(?:\((?:[^()\d]{3,120})\)\s*)?"
        r"(?:руб(?:\.|лей|ля|\b)|₽|тыс\.?\s*руб)",
        text,
        re.I,
    )
    if not with_currency:
        return None

    number = _to_number(with_currency.group(1))
    if number and 1_000 <= number <= 1_000_000_000:
        return number
    return None


def _document_streams(pack: EvidencePack) -> list[tuple[str, str]]:
    """Документы в виде (имя документа, сплошной текст).

    Требование собирается не из соседних элементов пакета, а из
    сплошного потока документа: в исковом заявлении между строками
    требования стоят другие фразы

        Взыскать с Фирма «...» в пользу Образцов Пётр Петрович
        Истец: Образцов Пётр Петрович, г.
        задолженность по договору поставки № Д-2026/001
        в размере 150000 руб.

    и склейка по соседству брала чужую строку вместо продолжения.

    Порядок внутри документа восстанавливается по полю order — пакет
    группируется по категориям и сам по себе порядка не хранит.
    """
    documents: list[str] = []
    for item in pack.items:
        if item.source_document not in documents:
            documents.append(item.source_document)

    streams: list[tuple[str, str]] = []
    for name in documents:
        parts = [item for item in pack.items if item.source_document == name]
        # order == 0 означает «порядок неизвестен»; такие элементы
        # оставляются в конец, чтобы известный порядок не ломался.
        parts.sort(key=lambda i: i.order if i.order else 10**9)
        streams.append((name, " ".join(part.text for part in parts)))
    return streams


# Где начинается требование. Окно требования идёт отсюда до следующего
# такого начала или до конца окна в 700 символов.
#
# Вводные глаголы («требует», «просит», «прошу») сюда НЕ включены
# намеренно: они стоят перед требованием, а не в нём. Если считать их
# началом окна, фраза «требует взыскать задолженность» режется на
# два окна — «требует» и «взыскать задолженность», и первое
# оказывается требованием без основания.
CLAIM_START = re.compile(
    r"(?:\bвзыска(?:ть|ть\b)|\bвзыскивани\w+|\bвозмести(?:ть|м|ть)\b|"
    r"\bобяза(?:ть|ть)\b|\bпризна(?:ть|ем|ет)\b|"
    # Требования, которые не относятся к взысканию денег. Они
    # попадут в список непознанных и будут видны судье — молча
    # пропущенное требование выглядит как «исков не было».
    r"\bпривлечь\b|\bвосстанови(?:ть|л)\b|\bотмени(?:ть|л)\b)",
    re.I,
)

# Окно одного требования. Достаточно для «взыскать с … о взыскании
# задолженности … в размере 150000 руб.», но не захватывает
# следующее требование, если оно начинается раньше.
CLAIM_WINDOW = 700

# Признак встречного требования. Слово «встречн» покрывает все три
# реальные формы, найденные в резолюциях: «встречный иск»,
# «встречное исковое заявление», «встречные исковые требования».
COUNTER_MARKER = re.compile(r"встречн\w*", re.I)


def _claim_windows(
    streams: list[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    """Нарезать сплошной текст на окна требований.

    Вместе с окном возвращается контекст перед ним (до 200 знаков):
    без него не видно, чьё это требование. «Просит взыскать …» —
    требование истца, а «Суд считает необходимым взыскать …» —
    определение суда, и сумма оттуда не должна попасть в требования
    истца (real-05: 1 000 руб. в мотивировке против 10 000 руб. в
    протоколе).
    """
    windows: list[tuple[str, str, str]] = []
    for name, text in streams:
        starts = [m.start() for m in CLAIM_START.finditer(text)]
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(text)
            chunk = text[start : min(end, start + CLAIM_WINDOW)]
            if chunk.strip():
                context = text[max(0, start - 200) : start]
                # Для различения «требование истца» и «определение суда»
                # важен только конец контекста: слова суда из соседнего
                # предложения не должны превращать требование истца в
                # приговор.
                tail = re.split(r"(?<=[.;])\s+", context)
                windows.append((name, chunk, tail[-1] if tail else ""))
    return windows


def _in_document_order(pack: EvidencePack) -> list[EvidenceItem]:
    """Элементы пакета в порядке их следования в документах.

    Пакет упорядочен по категориям, а не по документу. Для склейки
    требования это не годится: соседние элементы списка могут
    относиться к разным документам и разным частям требования.

    Порядок восстанавливается по двум признакам: сначала документы
    в порядке первого появления, затем номер предложения внутри
    документа (поле order). Элементы без номера (созданные вне пакета)
    ставятся в конец своей группы, чтобы не ломать склейку.
    """
    documents: list[str] = []
    for item in pack.items:
        if item.source_document not in documents:
            documents.append(item.source_document)

    def sort_key(item: EvidenceItem) -> tuple[int, int]:
        try:
            doc_index = documents.index(item.source_document)
        except ValueError:
            doc_index = len(documents)
        # order == 0 означает «порядок неизвестен»; такие элементы
        # уходят в конец, но внутри группы сохраняют исходный порядок.
        return doc_index, (item.order if item.order else 10**9)

    return sorted(pack.items, key=sort_key)


def _join_claim(items: list[EvidenceItem], position: int) -> str:
    """Склеить требование из соседних фраз пакета.

    Требование в заявлении печатается в несколько строк, а пакет
    хранит предложения по отдельности. Без склейки первая строка
    («Взыскать с ответчика задолженность...») остаётся без суммы и без
    основания, и в резолютивную часть попадает пустой пункт.

    Склеиваются не более двух следующих предложений и только из того
    же документа: больше — риск захватить требование из соседнего
    пункта, меньше — не хватает для «в размере 150000 руб.».
    """
    parts = [items[position].text]
    for offset in (1, 2):
        following = position + offset
        if following >= len(items):
            break
        if items[following].source_document != items[position].source_document:
            break
        # Строка с суммой заканчивает требование: склеивать её с
        # последующими нельзя, иначе «в размере 150000 руб.» присоединит
        # к себе следующее требование и неустойка получит чужую сумму.
        # Проверено на демо: неустойка показывала 150 000 вместо 90 000.
        if re.search(r"\d[\d\s]*\s*руб", items[position].text, re.I):
            break
        parts.append(items[following].text)
    return " ".join(parts)


def _shorten(text: str, limit: int = 200) -> str:
    """Сократить фразу до одного предложения — в проект решения длинное
    рассуждение попадать не должно."""
    first = re.split(r"(?<=[.;])\s", text.strip())[0]
    clean = re.sub(r"\s+", " ", first).strip()
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean


def _fee_from(items: list[EvidenceItem]) -> int | None:
    """Государственная пошлина из материалов дела."""
    for item in items:
        for pattern in FEE_PATTERNS:
            match = pattern.search(item.text)
            if match:
                value = _to_number(match.group(1))
                if value and 100 < value < 1_000_000:
                    return value
    return None


def _costs_from(items: list[EvidenceItem]) -> int | None:
    """Иные расходы из материалов дела.

    Сумма берётся после признака расходов, а не первая в предложении:
    в одной фразе протокола перечислены и неустойка, и затраты на
    экспертизу, и первой стоит неустойка.
    """
    for item in items:
        marker = COSTS_PATTERN.search(item.text)
        if marker is None:
            continue
        value = _find_amount(item.text[marker.end():])
        if value and 1_000 < value < 1_000_000_000:
            return value
    return None


def extract_requirements(
    pack: EvidencePack,
) -> tuple[list[MoneyClaim], list[Claimant], list[str]]:
    """Собрать требования из пакета доказательств.

    Возвращает тройку:
      money_claims — требования с денежной суммой;
      other_claims — требования без суммы (признать, обязать);
      unrecognised — фразы, похожие на требования, но не разобранные.
        Пустой список означает, что всё распознано. Непустой — сигнал
        судье, что раздел может быть неполным.

    Повторяющиеся основания объединяются: если истец просит взыскать
    задолженность в двух документах, это одно требование, а не два.
    Иначе в резолютивной части появились бы два пункта об одном и том же.

    ОГРАНИЧЕНИЕ, О КОТОРОМ ЧИТАТЕЛЬ ДОЛЖЕН ЗНАТЬ
        Пакет хранит предложения по отдельности, а требование в заявлении
        часто разбито на несколько строк:

            1. Взыскать с Фирма «...» в пользу Образцов
               задолженность по договору поставки
               в размере 150000 руб.

        По отдельности эти строки неразрешимы: в первой нет ни суммы, ни
        основания. Модуль склеивает соседние фразы (см. _join_claim),
        но если документ написан иначе, основание может не определиться.
        В таком случае требование попадает в unrecognised и судья видит
        его в проекте решения, а не теряет молча. Лучше видимое
        неполное требование, чем незаметно построенное не то.
    """
    money: dict[str, MoneyClaim] = {}
    others: dict[str, Claimant] = {}
    unrecognised: list[str] = []

    # Порядок элементов пакета НЕ совпадает с порядком в документе:
    # пакет группируется по категориям, и фраза из середины требования
    # оказывается рядом с чужой. Поэтому требования склеиваются по
    # полю order — это номер предложения в исходном документе.
    #
    # Порядок в документе важен потому, что требование в заявлении
    # разбито на строки:
    #
    #     Взыскать с ответчика задолженность по договору поставки
    #     в размере 150000 руб.
    #
    # Первая строка попадает в «claims», вторая — в «evidence»
    # (там есть слово «руб»), и без общего порядка сумма терялась.
    for source, text, context in _claim_windows(_document_streams(pack)):
        if _is_court_determination(context):
            # Это не требование истца, а определение суда («суд
            # считает необходимым взыскать …»). Сумма оттуда в
            # требования истца не попадает: иначе проект просит то,
            # что присудил суд, и выглядит как требование.
            continue

        requirement_text = text
        if _is_fee_requirement(text):
            # Пошлина взыскивается отдельным пунктом по ст. 98 ГПК РФ
            # и не должна занимать место требования. Прежде окно
            # пропускалось целиком, и пошлина, стоящая в одном
            # предложении с требованием, топила его целиком: в деле
            # real-08 фраза «расходы по оплате госпошлины в размере
            # 416 руб.» стояла в одной строке с ущербом 10 400 руб.,
            # и система теряла взыскание долга. Пошлинные куски
            # вырезаются (по предложениям, запятым и союзам), остаток
            # разбирается дальше. Вырезание повторяется, потому что
            # пошлина встречается разными формами («госпошлина»,
            # «судебные расходы», «расходы по уплате пошлины»).
            requirement_text = _without_fee_fragments(text)
            for _ in range(2):
                if not _is_fee_requirement(requirement_text):
                    break
                requirement_text = _without_fee_fragments(requirement_text)
            if not requirement_text or not looks_like_claim(requirement_text):
                # Чисто пошлинное окно (пустой остаток) — не требование
                # по существу, выбрасывается по-прежнему. Если же в
                # остатке есть число из материалов, но не похоже на
                # требование, — не молчим: фрагмент показывается
                # судье, иначе вырезание снова превращается в тихую
                # потерю суммы.
                if requirement_text and re.search(r"\d", requirement_text):
                    unrecognised.append(_shorten(requirement_text))
                continue

        if not requirement_text.strip():
            continue
        if _is_fee_requirement(requirement_text):
            continue

        # Пункты перечисления внутри окна: у каждого своё основание и
        # своя сумма. Разбор окна целиком давал одно основание, и
        # остальные суммы терялись (real-03: неустойка 18 390;
        # real-04: стоимость товара 10 490 и услуги 7 000).
        #
        # Платёж ОТ истца требованием не является: окно с таким
        # платежом в разборе по пунктам пропускается, как и целиком.
        #
        # Сумма в окне принадлежит одному основанию — но только для
        # запасного пути «первая сумма окна». В разборе по пунктам у
        # каждого пункта своя сумма, и одинаковые суммы у разных
        # оснований — не двойной счёт, а два требования («убытки
        # 5 000 рублей и неустойку 5 000 рублей»).
        used_amounts: dict[tuple[int, int], str] = {}
        clause_text = "" if payer_is_plaintiff(requirement_text) else requirement_text
        clause_result = _clause_claims(clause_text)
        for clause_name, clause_whole, clause_kopecks in clause_result.claims:
            if clause_name in NON_MONEY_BASES:
                # Требование без суммы («проценты … по дату фактического
                # исполнения»). Сумма в окне ему НЕ принадлежит: там
                # стоит основной долг, и приписывание его текущим
                # процентам давало в резолюции два пункта с одной суммой.
                # Такое требование разрешается словами, по основанию
                # окна (см. ветку NON_MONEY_BASES ниже).
                continue
            used_amounts[(clause_whole, clause_kopecks)] = clause_name
            existing_clause = money.get(clause_name)
            if existing_clause is None or existing_clause.amount is None:
                money[clause_name] = MoneyClaim(
                    amount=clause_whole,
                    kopecks=clause_kopecks,
                    basis=clause_name,
                    source=source,
                    outcome=Outcome.SATISFIED,
                )
            elif (
                existing_clause.amount != clause_whole
                or existing_clause.kopecks != clause_kopecks
            ):
                # Одно основание — две разные суммы. Это либо
                # уточнённое требование (real-01: иск 11 107, протокол
                # 7 919), либо два пункта одного перечисления (real-04:
                # неустойка 2 202,90 и уточнённая 10 564,40). Решение —
                # за судьёй, но молчать нельзя.
                #
                # Определения суда в требования не попадают (см.
                # _is_court_determination), поэтому расхождение между
                # иском и решением суда сюда не доходит.
                _add_note(
                    unrecognised,
                    _conflict_note(
                        clause_name,
                        int(existing_clause.amount),
                        existing_clause.kopecks,
                        existing_clause.source,
                        clause_whole,
                        clause_kopecks,
                        source,
                    ),
                )
        for blank_name in clause_result.blanks:
            # Требование без своей суммы: остаётся видимым (в
            # резолюции по нему — пробел «не указана сумма»), а не
            # исчезает молча.
            if blank_name in NON_MONEY_BASES:
                # Без суммы и не должен её иметь: разрешается словами.
                continue
            if blank_name not in money:
                money[blank_name] = MoneyClaim(
                    amount=None,
                    kopecks=0,
                    basis=blank_name,
                    source=source,
                    outcome=Outcome.SATISFIED,
                )
        for conflict_name, first_sum, first_kop, second_sum, second_kop in (
            clause_result.conflicts
        ):
            _add_note(
                unrecognised,
                _conflict_note(
                    conflict_name,
                    first_sum,
                    first_kop,
                    source,
                    second_sum,
                    second_kop,
                    source,
                ),
            )

        basis_match = claim_basis_match(requirement_text)
        basis = basis_match[0] if basis_match is not None else None
        if basis is None:
            # Требование в долевой форме: «взыскать 1/3 часть от суммы
            # ...». Рублёвой суммы требования в материалах может не
            # быть — в real-10 она обезличена (<ОБЕЗЛИЧИНО> рублей
            # 49 копеек), и окно уходило в нераспознанные. Проект
            # выдавал «резолютивная часть не сформирована» — иск
            # выглядел не поданным, хотя он подан.
            #
            # Требование остаётся видимым без суммы: в резолютивной
            # части судья видит ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ и сумму
            # подставляет сам. Вычислять долю из55 021,47 нельзя:
            # система не считает чисел, которых нет в материалах.
            shares_added = False
            for fraction in _SHARE_FRACTION.finditer(requirement_text):
                # Доля входит в основание: два долевых требования
                # («1/3 часть» и «1/2 часть») — два пункта, а не один
                # с потерей второй доли (находка независимого ревью).
                # Обе доли ищутся по всему окну: в перечислении «просит
                # взыскать 1/3 часть …, а также 1/2 часть …» они стоят
                # в одном окне, и первая перекрывала вторую.
                share_basis = f"расходы в долевой части {fraction.group(1)}"
                if share_basis not in money:
                    money[share_basis] = MoneyClaim(
                        amount=None,
                        kopecks=0,
                        basis=share_basis,
                        source=source,
                        outcome=Outcome.SATISFIED,
                    )
                shares_added = True
            if shares_added:
                continue
            # Требование есть, а основание определить не удалось.
            # Оно показывается судье, а не выбрасывается: молча
            # потерянное требование выглядит как «исков не было».
            unrecognised.append(_shorten(requirement_text))
            continue

        if payer_is_plaintiff(requirement_text):
            continue

        # Признание и обязательство совершить действие денег не имеют.
        if basis in NON_MONEY_BASES:
            if basis not in others:
                others[basis] = Claimant(
                    description=f"{basis}",
                    source=source,
                    outcome=Outcome.SATISFIED,
                )
            continue

        # Копейки ищутся вместе с рублями: «5034 руб. 79 коп.» —
        # одна сумма, и терять вторую часть нельзя.
        #
        # Первое окно базы без суммы не блокирует последующее окно с
        # суммой. Прежде база занималась навсегда: «требования истца
        # о компенсации морального вреда являются обоснованными»
        # (без числа) стояло раньше окна с суммой 1000 руб. и
        # оставляло моральный вред без суммы, хотя в иске она есть
        # (real-05).
        #
        # Сумма берётся та, что стоит ПОСЛЕ основания, а не первая в
        # окне. В окне с несколькими пунктами первая сумма — соседняя:
        # «взыскать долг 5000 руб. и неустойку 1000 руб.» отдавало
        # неустойке 5000, а в real-03 сумма неустойки 18 390 доставалась
        # компенсации морального вреда. Если после основания суммы нет
        # («взыскать 1000 руб. в возмещение убытков»), остаётся первая
        # сумма окна.
        found = _find_amount_with_kopecks(requirement_text)
        if basis_match is not None:
            after_basis = _find_amount_with_kopecks(requirement_text[basis_match[1].end():])
            if after_basis is not None:
                found = after_basis
        whole, kopecks = found if found else (None, 0)
        if whole is not None:
            owner = used_amounts.get((whole, kopecks))
            if owner is not None and owner != basis:
                # Сумма уже отдана другому основанию этого окна: это
                # запасной путь «первая сумма окна», и она чужая.
                # Требование остаётся видимым, но без своей суммы.
                if basis not in money:
                    money[basis] = MoneyClaim(
                        amount=None,
                        kopecks=0,
                        basis=basis,
                        source=source,
                        outcome=Outcome.SATISFIED,
                    )
                continue
            used_amounts[(whole, kopecks)] = basis
        existing = money.get(basis)
        if existing is None or (existing.amount is None and whole is not None):
            money[basis] = MoneyClaim(
                amount=whole,
                kopecks=kopecks,
                basis=basis,
                source=source,
                outcome=Outcome.SATISFIED,
            )
        elif (
            existing.amount is not None
            and whole is not None
            and (existing.amount != whole or existing.kopecks != kopecks)
        ):
            # Одно основание — две разные суммы (см. выше).
            _add_note(
                unrecognised,
                _conflict_note(
                    basis,
                    int(existing.amount),
                    existing.kopecks,
                    existing.source,
                    whole,
                    kopecks,
                    source,
                ),
            )

    # Размер пошлины и иных расходов возвращаются отдельно, через
    # extract_fee_and_costs: они относятся не к требованиям истца, а к
    # распределению расходов, и в раздел «требования» попадать не должны.
    #
    # Пробелы пакета («нет данных: установленные обстоятельства») сюда
    # НЕ подмешиваются: это описание материалов, а не неразобранные
    # требования. Раньше они попадали в список неразобранных и в
    # резолютивной части появлялся пункт «нет данных: установленные
    # обстоятельства», которого в иске не было.

    # Исход отражается из материалов дела, если он там есть (см.
    # resolve_outcomes). Требования без исхода в материалах остаются
    # требованиями истца — решение принимает судья.
    resolved = resolve_outcomes(pack, list(money.values()))
    money = {claim.basis: claim for claim in resolved}

    return list(money.values()), list(others.values()), unrecognised


#: Установительная (мотивировочная) часть решения суда.
#:
#: Файл court_decision_reference.txt НЕ читается намеренно: это ключ
#: к делу — сам ответ суда, по которому проверяется работа проекта.
#: Исход, вычитанный из ключа, превратил бы проверку в самопроверку.
_FINDINGS_MARKER = re.compile(
    r"считает\s+(?:иск|требован|исков)"
    r"|подлежащ\w*\s+удовлетворени"
    r"|удовлетвор\w*\s+частично"
    r"|установленн\w*\s+обстоятель"
    r"|исследовав\s+(?:доказатель|материал)"
    r"|заслушав\s+(?:объяснени|пояснени)",
    re.I,
)

_REFERENCE_DOCUMENT = re.compile(r"court_decision_reference", re.I)

#: Требование в долевой форме: «взыскать 1/3 часть от суммы ...»,
#: «взыскивает 1/2 части». Сумма требования здесь — не рубль, а доля,
#: и рублёвого числа требования в материалах может не быть.
#: В группу 1 захватывается сама доля: она входит в основание пункта.
_SHARE_FRACTION = re.compile(r"(\d+\s*/\s*\d+)\s+част", re.I)

#: Заголовок мотивировочной части — «УСТАНОВИЛ:» отдельной строкой или
#: шапка «ОБСТОЯТЕЛЬСТВА, УСТАНОВЛЕННЫЕ СУДОМ». Без требования заголовка
#: в установительную часть попал протокол судебного заседания: там
#: цитируется иск («Просит ... взыскать ... 9 000 руб.»), и цитата
#: подтверждала исход требования самим иском — суд «удовлетворял» то,
#: что истец просил, а решение ещё не принято.
_FINDINGS_HEADING = re.compile(
    r"^\s*УСТАНОВИЛ\s*:?\s*$|ОБСТОЯТЕЛЬСТВА,\s*УСТАНОВЛЕННЫЕ\s+СУДОМ",
    re.I | re.M,
)

#: Материалы прямо говорят о частичном удовлетворении: либо общая
#: формулировка («исков подлежит удовлетворению частично»), либо
#: отказ в остальной части.
_PARTIAL_SIGNAL = re.compile(
    r"удовлетвор\w*\s+частично"
    r"|в\s+остальной\s+части[^.]{0,120}отказать",
    re.I,
)

#: Процентный штраф (ст. 13 ЗПП): 50 % присужденных сумм идёт
#: потребителю и организации по защите прав потребителей. Это
#: санкция суда, а не неустойка истца: фраза со штрафом не должна
#: подтверждать исход требования о неустойке.
_SANCTION_PERCENT = re.compile(r"штраф\w*[^.]{0,40}\d+\s*%", re.I)

#: Отказная формулировка по конкретному основанию. Фрагмент с ней не
#: подтверждает исход: раньше «Требование истца о взыскании неустойки
#: в размере 10 000 руб. удовлетворению не подлежит» содержал и
#: основание, и сумму, и считался подтверждением — проект писал «в
#: полном объёме» там, где суд отказал (находка независимого ревью).
#: Общая «В остальной части иска отказать» сюда не попадает: это
#: сигнал частичности, а не отказ по конкретному основанию.
_REFUSAL_SIGNAL = re.compile(
    r"удовлетворени\w*\s+не\s+подлежит"
    r"|не\s+удовлетворять"
    r"|в\s+удовлетворени\w+[^.]{0,120}?отказать"
    r"|отказать\s+в\s+удовлетворени"
    # «Оснований для взыскания ... не имеется» — тоже отказ, форма из
    # real-01 (суд отказал по ремонту 7 919,17).
    r"|основани\w*[^.]{0,120}?не\s+имеется"
    r"|не\s+имеется\s+основани",
    re.I,
)


def _findings_streams(pack: EvidencePack) -> list[tuple[str, str]]:
    """Документы с установительной частью решения суда.

    Берутся сплошные тексты (pack.documents), а не размеченные фразы
    пакета: классификатор оставляет по одному признаку на фразу, и
    ключевые предложения установительной части («исков подлежит
    удовлетворению частично», «взыскивает моральный вред в сумме
    2000 рублей») в размеченных фразах отсутствуют. По делу real-06
    пакет хранил 1 241 символ из 4 255 — исход по ним не определялся
    вовсе.

    Документ должен нести заголовок установительной части
    («УСТАНОВИЛ:» / «ОБСТОЯТЕЛЬСТВА, УСТАНОВЛЕННЫЕ СУДОМ»). Без него
    протокол заседания попадал сюда же: там цитируется иск («просит
    взыскать ... 9 000 руб.»), и цитата подтверждала исход требования
    самим иском — суд «удовлетворял» то, что истец просил, хотя
    решение ещё не принято.
    """
    streams = (
        list(pack.documents.items())
        if pack.documents
        else _document_streams(pack)
    )
    return [
        (source, text)
        for source, text in streams
        if not _REFERENCE_DOCUMENT.search(source)
        and _FINDINGS_HEADING.search(text)
        and _FINDINGS_MARKER.search(text)
    ]


def resolve_outcomes(
    pack: EvidencePack, claims: list[MoneyClaim]
) -> list[MoneyClaim]:
    """Отразить исход требования, если он есть в материалах дела.

    Раньше исход по умолчанию был «удовлетворено» для каждого
    требования, и проект писал «в полном объёме» там, где суд отказал
    в части (real-06: суд взыскал моральный вред 2 000 из заявленных
    7 000 и отказал в остальной части). Это решение суда, которое
    подготовительная машина выдавала за своё.

    Правила — только по материалам, без догадок:

    1. В установительной части нашлось основание требования с той же
       суммой — исход подтверждён, требование удовлетворяется.
    2. Нашлось основание с меньшей суммой — частичное удовлетворение
       с указанием суммы, установленной судом.
    3. Материалы прямо говорят о частичном удовлетворении («исков
       подлежит удовлетворению частично», «в остальной части отказать»),
       а основание в установительной части не нашлось — исход
       неизвестен: требование помечается как частичное без суммы, и в
       резолюции появляется пробел, который судья заполнит сам.
    4. Сигналов нет (дело ещё не решено) — исход не меняется:
       требование остаётся тем, что просил истец.

    Установительная часть не читается из court_decision_reference.txt:
    это ключ к делу, и исход из него сделал бы проверку самопроверкой.
    """
    findings = _findings_streams(pack)
    if not findings or not claims:
        return claims

    findings_text = "\n".join(text for _source, text in findings)
    findings_source = findings[0][0]
    partial_signal = bool(_PARTIAL_SIGNAL.search(findings_text))

    # Суммы, которые суд установил по каждому основанию, и основания,
    # по которым суд прямо отказал. Фильтры одинаковые для обеих
    # таблиц: штраф ст. 13 ЗПП и отказные конструкции не подтверждают
    # исход требования, а отказная — прямо его отрицает.
    granted: dict[str, list[tuple[int, int]]] = {}
    refused: set[str] = set()
    for fragment in _fragments(findings_text):
        if _SANCTION_PERCENT.search(fragment):
            continue
        for pattern, basis in CLAIM_BASIS_ANCHORS:
            match = pattern.search(fragment)
            if match is None:
                continue
            # Сумма ищется после основания: в предложении «суд
            # взыскивает моральный вред в сумме 2 000 рублей» она
            # стоит позади, а первая сумма предложения могла бы
            # принадлежать соседнему пункту.
            found = _find_amount_with_kopecks(fragment[match.end() :])
            if found is None:
                found = _find_amount_with_kopecks(fragment)
            if _REFUSAL_SIGNAL.search(fragment):
                # «Требование о взыскании неустойки 10 000 руб.
                # удовлетворению не подлежит» — суд отказал именно в
                # этом требовании: основание и сумма совпали.
                refused.add(basis)
            elif found is not None:
                granted.setdefault(basis, []).append(found)
            break

    resolved: list[MoneyClaim] = []
    for claim in claims:
        if claim.amount is None:
            # Требование без суммы: сравнивать не с чем, исход
            # остаётся на усмотрение судьи.
            resolved.append(claim)
            continue

        claim_key = (int(claim.amount), claim.kopecks)
        amounts = granted.get(claim.basis, [])

        if claim_key in amounts:
            # Удовлетворение подтверждено материалами: сумма суда
            # совпала с суммой требования.
            resolved.append(claim)
            continue

        if claim.basis in refused:
            # Суд отказал в этом требовании прямо (см. _REFUSAL_SIGNAL).
            # Исход берётся из материалов, а не из пожелания истца.
            resolved.append(
                replace(
                    claim,
                    outcome=Outcome.DENIED,
                    grounds=(
                        "материалы указывают на отказ в удовлетворении "
                        f"(см. {findings_source})"
                    ),
                )
            )
            continue

        smaller = [key for key in amounts if key < claim_key]
        if smaller:
            granted_whole, granted_kop = max(smaller)
            resolved.append(
                replace(
                    claim,
                    outcome=Outcome.PARTIAL,
                    granted_amount=float(granted_whole),
                    granted_kopecks=granted_kop,
                    grounds=f"сумма, установленная судом (см. {findings_source})",
                )
            )
            continue

        if amounts:
            # Сумма суда больше требуемой — такого быть не должно.
            # Исход «в полном объёме» здесь лгал бы: суд взыскал не то,
            # что просили. Требование становится частичным с суммой
            # суда — обе цифры видны в пункте, расхождение сверяет
            # судья (находка независимого ревью: раньше оставался
            # дефолт SATISFIED без единой пометки).
            court_whole, court_kop = max(amounts)
            resolved.append(
                replace(
                    claim,
                    outcome=Outcome.PARTIAL,
                    granted_amount=float(court_whole),
                    granted_kopecks=court_kop,
                    grounds=(
                        "сумма суда больше заявленной "
                        f"(см. {findings_source}) — сверьте"
                    ),
                )
            )
            continue

        if partial_signal:
            resolved.append(
                replace(
                    claim,
                    outcome=Outcome.PARTIAL,
                    granted_amount=None,
                    grounds=(
                        "материалы указывают на частичное удовлетворение, "
                        f"сумма по основанию не установлена (см. {findings_source})"
                    ),
                )
            )
            continue

        resolved.append(claim)
    return resolved


def _fragments(text: str) -> list[str]:
    """Предложения установительной части: по ним опознаётся основание."""
    return [
        piece.strip()
        for piece in re.split(r"(?:[.!?]|\n)+", text)
        if len(piece.strip()) > 15
    ]


def extract_counter_claims(pack: EvidencePack) -> list[CounterClaim]:
    """Встречные требования ответчика из материалов дела.

    Встречный иск меняет направление платежа: ответчик становится
    истцом. В решениях он формулируется как «В удовлетворении встречных
    исковых требований … отказать» или «Взыскать с истца в пользу
    ответчика …».

    ПОЧЕМУ ИЩЕМ НЕ ПО ОКНАМ ТРЕБОВАНИЙ. Первая версия брала
    текст требования из _claim_windows, и на живом тексте не срабатывала
    ни разу. Окно начинается с глагола «взыскать», а признак стоит
    перед ним: «Ответчик заявил встречный иск: взыскать с истца …».
    Признак отсекался, и резолюция выходила без пункта — как будто
    ответчик ничего не заявлял. Поэтому окно строится от самого слова
    «встречн», а не от глагола.

    Направление платежа определяется по плательщику: если в тексте «с
    истца», требование предъявлено ответчиком. Если плательщик не
    определён, а требование отказано, направление не нужно — суд
    отказал, и перечислять стороны незачем.
    """
    counter: list[CounterClaim] = []
    seen: set[tuple[str, int]] = set()
    for source, stream in _document_streams(pack):
        for match in COUNTER_MARKER.finditer(stream):
            window = stream[match.start() : match.start() + CLAIM_WINDOW]
            low = window.lower()
            # Отказ судьи в принятии заявления — это не требование,
            # а процессуальное действие. В резолюции ему не место.
            if "принять встречн" in low and "требован" not in low:
                continue
            denied = "отказать" in low
            if not denied and not payer_is_plaintiff(window):
                continue
            key = (source, match.start())
            if key in seen:
                continue
            seen.add(key)
            found = _find_amount_with_kopecks(window)
            whole, kopecks = found if found else (None, 0)
            counter.append(
                CounterClaim(
                    amount=whole,
                    basis=claim_basis(window) or "встречное требование",
                    source=source,
                    kopecks=kopecks,
                    outcome=Outcome.DENIED if denied else Outcome.SATISFIED,
                )
            )
    return counter


def extract_procedure(pack: EvidencePack) -> Procedure:
    """Порядок рассмотрения дела по материалам.

    Заочное производство — 49 % гражданских дел по разбору 1 425
    решений, поэтому определение порядка существенно: без него
    резолютивная часть получается неполной.

    Порядок устанавливается по материалам дела, а не по умолчанию.
    Два правила, выведенных из живого дела 1w121:

    1. Фраза «Возражений против рассмотрения дела в порядке заочного
       производства не имею» — согласие стороны, а не определение суда.
       Раньше она считалась назначением заочного производства.
    2. Заочное — это неявка ОТВЕТЧИКА (ст. 233 ГПК РФ). Заявление
       истца «рассмотреть дело в моё отсутствие» относится к ст. 167 и
       дело заочным не делает.

    Поэтому заочное ставится только при явном признаке неявки
    ответчика или когда дело уже рассмотрено в таком порядке; без них —
    обычный порядок, а не выдуманная заочность.
    """
    text = " ".join(item.text.lower() for item in pack.items)

    # Согласие вместо назначения (см. правило 1).
    consent = bool(
        re.search(
            r"возражени\w*[^\n]{0,160}?"
            r"в\s+порядке\s+заочного\s+производства[^\n]{0,80}?"
            r"не\s+(?:имею|возража|выража)"
            r"|не\s+возража\w+[^\n]{0,160}?заочн",
            text,
        )
    )

    # Признаки того, что суд уже решил вопрос или ответчик не явился:
    # без одного из них заочное в проект не идёт.
    absence = bool(
        re.search(
            # определение суда: «Суд определил рассмотреть дело в
            # порядке заочного производства…»
            r"определил[^\n]{0,120}?в\s+порядке\s+заочного"
            r"|рассмотреть\s+дело\s+в\s+порядке\s+заочного"
            r"|рассмотрен\w+\s+(?:дело\s+)?в\s+порядке\s+заочного"
            r"|дело\s+рассматривается\s+в\s+порядке\s+заочного"
            # неявка ответчика — основание ст. 233 ГПК РФ
            r"|(?:заочн\w*|извещен\w*)[^\n]{0,160}?в\s+отсутствие\s+ответчика"
            r"|ответчик[^\n]{0,160}?"
            r"(?:не\s+явил\w*|не\s+извещен|не\s+прожива|не\s+сообщил\w*|отсутству)"
            r"|неявк\w*[^\n]{0,160}?ответчик",
            text,
        )
    )

    if absence and not consent:
        return Procedure.DEFAULT

    # Отсутствие истца — другая норма (ст. 167 ГПК РФ): в материалах
    # заявление идёт «от истца», а по ст. 233 решается неявка
    # ответчика. Путать их нельзя.
    if re.search(
        r"рассмотреть\s+дело\s+в\s+отсутствие\s+истца"
        r"|рассмотреть\s+дело\s+в\s+мо[ёе]\s+отсутствие"
        r"|без\s+участия\s+\(\s*от\s+истца\s*\)",
        text,
    ):
        return Procedure.ABSENT_PLAINTIFF

    return Procedure.ORDINARY


def extract_budget(pack: EvidencePack) -> str:
    """Бюджет, в доход которого взыскивается госпошлина.

    По закону госпошлина возвращается бюджету, а не истцу (ст. 98
    ГПК РФ), поэтому в резолюции она указывается с адресом бюджета:
    «в доход бюджета городского округа Самара». Без адресата пункт
    о пошлине неполон.

    Бюджет берётся из материалов дела. Если в материалах его нет,
    возвращается пустая строка, и на вызывающем коде появляется
    видимая пометка: выдумывать бюджет по умолчанию нельзя, потому
    что ошибка приводит к взысканию не в тот адресат.
    """
    text = " ".join(item.text for item in pack.items)

    # Явная формулировка из материалов — самый надёжный источник.
    match = re.search(
        r"доход\s+бюджета\s+([^\s,.;:]{2,60}"
        r"(?:[\s\-][^\s,.;:]{2,40})*)",
        text,
        re.I,
    )
    if match:
        budget = re.sub(r"\s+", " ", match.group(1)).strip(" .,;:")
        if budget:
            return budget

    # Иначе — по названию суда: суд и район его делопроизводства
    # определяют бюджет, в который идёт пошлина.
    match = re.search(r"в\s+(?:доход\s+)?(?:бюджета\s+)?"
                      r"([А-ЯЁ][а-яё\-]+\s+(?:области|края|города|городского\s+округа))",
                      text)
    if match:
        return match.group(1).strip()
    return ""


# Признаки пошлины и судебных расходов — общие для проверки окна
# (_is_fee_requirement) и для вырезания (_without_fee_fragments):
# один источник, чтобы вырезание не расходилось с проверкой.
_FEE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"госпошлин\w*", re.I),
    re.compile(r"судебн\w*\s+расход\w*|судебн\w*\s+издержк\w*", re.I),
    re.compile(
        r"расход\w*\s+(?:на|по)\s+(?:уплат\w+\s+)?"
        r"(?:государственн\w*\s+)?пошлин",
        re.I,
    ),
)

# Окно режется по границам, на которых можно безопасно отделить
# пошлину от денежного требования: конец предложения, запятая,
# сочинительные союзы «и» и «а также» (перечисление требований).
# Граница предложения — только если дальше заглавная: сокращения
# вроде «416 руб. 00 коп.» иначе расщепляются, и копейки от
# пошлины приклеиваются к соседней сумме.
_FEE_CHUNK_SPLIT = re.compile(
    r"((?<=[.;])\s+(?=[А-ЯЁ])|;\s*|,\s*|\s+и\s+|\s+а\s+также\s+)",
    re.I,
)


def _fee_marker_span(text: str) -> re.Match[str] | None:
    """Самый ранний признак пошлины/судебных расходов в тексте окна.

    Взять нужно первый по позиции, а не первый по списку: «расходы
    по уплате государственной пошлины» содержит и «госпошлин», но
    обрезать надо от начала всей конструкции, иначе после вырезания
    останется огрызок «расходы по уплате государственной».
    """
    earliest: re.Match[str] | None = None
    for marker in _FEE_MARKERS:
        match = marker.search(text)
        if match is not None and (earliest is None or match.start() < earliest.start()):
            earliest = match
    return earliest


def _is_fee_requirement(text: str) -> bool:
    """Требование о пошлине или судебных расходах.

    Такие требования не имеют самостоятельного предмета: пошлина
    взыскивается отдельным пунктом по ст. 98 ГПК РФ, а в списке
    требований по существу она не должна занимать место.
    """
    return _fee_marker_span(text) is not None


def _without_fee_fragments(text: str) -> str:
    """Вырезать пошлинные куски из окна требования.

    Раньше окно пропускалось целиком, если в нём упоминалась пошлина,
    но пошлина часто стоит в одном перечислении с денежными
    требованиями («ущерб в размере 10 400 руб., расходы по оплате
    госпошлины в размере 416 руб.») — и вместе с окном терялось само
    требование (real-08).

    Теперь окно режется по границам из _FEE_CHUNK_SPLIT; кусок
    выкидывается, если содержит признак пошлины. Когда признак стоит
    в середине куска без собственных границ («взыскать госпошлину и
    ущерб»), кусок обрезается до признака: «взыскать» остаётся,
    пошлина уходит. Остаток без признаков требования и без числа
    (огрызок вроде «расходы по оплате») выбрасывается — он вошёл бы
    в окно мусором. Если после вырезания остаток перестал быть
    похож на требование, вызывающий код не молчит: показывает
    фрагмент судье как нераспознанный.
    """
    chunks = _FEE_CHUNK_SPLIT.split(text)
    parsed: list[tuple[str, re.Match[str] | None, str]] = []
    for index in range(0, len(chunks), 2):
        segment = chunks[index].strip()
        carry = chunks[index - 1] if index > 0 else ""
        if not segment:
            continue
        parsed.append((segment, _fee_marker_span(segment), carry))
    # Цифры вне пошлинных кусков: пошлина стоит в одном перечислении
    # с денежным требованием. Огрызок «взыскать» сохраняется только
    # при таком соседе — иначе в окне была одна пошлина, и обрезанный
    # глагол превратился бы в шум в списке непознанных.
    neighbour_digits = re.compile(r"\d")
    has_neighbour_digits = any(
        neighbour_digits.search(segment)
        for segment, marker, _carry in parsed
        if marker is None
    )
    kept: list[str] = []
    for segment, marker, carry in parsed:
        if marker is None:
            kept.append((carry if kept else "") + segment)
            continue
        # Вырезанное место отмечается точкой с запятой: без неё куски
        # срастаются, и якорь из соседнего пункта («штраф») приклеивается
        # к чужой сумме (real-06: 9 000 руб. за услуги представителя
        # приклеились к неустойке).
        kept.append("; ")
        remainder = segment[: marker.start()].rstrip(" \t,;")
        if remainder and neighbour_digits.search(remainder):
            # Признак в середине куска с суммой до него: «взыскать
            # долг 5000 руб госпошлину» → хвост отрезается, долг
            # остаётся.
            kept.append((carry if kept else "") + remainder)
        elif remainder and CLAIM_VERB_RE.search(remainder) and has_neighbour_digits:
            # «взыскать госпошлину и ущерб 10400 руб.» → «взыскать и
            # ущерб»: глагол требования надо сохранить, рядом есть
            # требование с суммой.
            kept.append((carry if kept else "") + remainder)
    cleaned = re.sub(r"\s{2,}", " ", "".join(kept))
    return cleaned.strip(" \t,;")


# Граница предложения: точка и дальше заглавная. «руб.» и «коп.» так
# не разрывают предложение — иначе копейки отрывались бы от рублей.
_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+(?=[А-ЯЁ])")

# Формулировки суда, по которым он САМ определяет, что взыскать.
# Отличие от требования истца видно по словам перед глаголом:
# «просил взыскать» — истец, «суд считает необходимым взыскать» —
# суд. Слова «суд» самого по себе недостаточно: в протоколе истец
# говорит «просил суд принять отказ …, взыскать с ответчика …».
_COURT_DETERMINATION_RE = re.compile(
    r"суд(?:ом)?\b[^.]{0,120}?(?:считает\s+необходимым|определил|постановил|"
    r"присудил|решил|удовлетворил|отказал|признал)"
    r"|считает\s+необходимым"
    r"|необходимо\s*$"
    r"|подлежит\s+взысканию"
    r"|подлежат\s+взысканию",
    re.I,
)

# Истец в именительном падеже — значит, говорит он, а не суд. «В пользу
# истца» (родительный) — суд формулирует приговор, истец — нет.
_ACTOR_IS_PLAINTIFF_RE = re.compile(r"\bистец\b", re.I)


def _is_court_determination(context: str) -> bool:
    """Определение суда, а не требование истца.

    Смотрится только последнее предложение перед окном: «суд считает
    необходимым взыскать …» — это мотивировка, «просил взыскать …» —
    требование. Смотреть дальше нельзя: определение суда в соседнем
    предложении не делает требованием истца следующее окно.

    Если в этом предложении истец назван как действующее лицо
    («истец считает, что необходимо взыскать …»), требование остаётся
    требованием истца.
    """
    if _ACTOR_IS_PLAINTIFF_RE.search(context):
        return False
    return bool(_COURT_DETERMINATION_RE.search(context))


# Признаки того, что пункт — это обстоятельства, а не требование.
# В таких местах суд пересказывает установленное: «судом установлено,
# что ущерб истцу составил 7777 рублей». Без отсева такая сумма
# попадала бы в проект как требование (дефект ревью D2).
_NARRATION_MARKERS = re.compile(
    r"суд(?:ом)?\s+(?:установил|установлено|считает|пришел\s+к\s+выводу)"
    r"|установлено,?\s+что"
    r"|по\s+(?:мнению|заключению|показаниям)"
    r"|истец\s+пояснил"
    r"|принимая\s+во\s+внимание"
    r"|считается,?\s+что",
    re.I,
)


def _add_note(notes: list[str], text: str) -> None:
    """Добавить заметку, не повторяя уже добавленную.

    Один и тот же конфликт встречается в нескольких окнах (протокол
    повторяет требования), и судье достаточно увидеть его один раз.
    """
    if text not in notes:
        notes.append(text)


def _conflict_note(
    basis: str,
    first: int,
    first_kopecks: int,
    first_source: str,
    second: int,
    second_kopecks: int,
    second_source: str,
) -> str:
    """Заметка для судьи: по одному основанию в материалах разные суммы.

    Молчать здесь нельзя: в проекте стоит одна сумма, а судья видит
    в материалах другую. Копейки в заметке тоже: в проекте стоит
    2 202,90, и «2 202» в заметке смотрелось бы как опечатка.

    Формулировка намеренно осторожная: по основанию «задолженность по
    договору» две суммы в разных документах могут быть двумя разными
    задолженностями (в real-01 — ЖКУ за один период и ремонт за
    другой), а не уточнением одной. Что просить — решает судья.
    """
    def number(value: int, kopecks: int) -> str:
        whole = f"{value:,}".replace(",", " ")
        if kopecks:
            return f"{whole},{kopecks:02d}"
        return whole

    return (
        f"по требованию «{basis}» в материалах разные суммы: "
        f"{number(first, first_kopecks)} руб. ({first_source}) и "
        f"{number(second, second_kopecks)} руб. ({second_source}) — "
        f"уточнение или разные требования, решает судья"
    )


def _clauses(text: str) -> list[str]:
    """Разбить окно требования на пункты перечисления.

    Границы те же, что и при вырезании пошлинных кусков: конец
    предложения (перед заглавной — «руб.» и «коп.» не разрываются),
    запятая, союзы «и» и «а также».
    """
    parts = _FEE_CHUNK_SPLIT.split(text)
    return [parts[index].strip() for index in range(0, len(parts), 2) if parts[index].strip()]


def _first_amount_span(text: str) -> tuple[int, int] | None:
    """Границы первого денежного размера в тексте."""
    match = re.search(
        r"([\d][\d\s]{2,})\s*(?:\((?:[^()\d]{3,120})\)\s*)?"
        r"(?:руб(?:\.|лей|ля|\b)|₽|тыс\.?\s*руб)",
        text,
        re.I,
    )
    if match is None:
        return None
    return match.start(), match.end()


def _clause_basis(clause: str) -> str | None:
    """Основание пункта перечисления.

    Берётся якорь, ближайший к сумме и стоящий перед ней: в пункте
    «расходы на юридические услуги и представительство … в сумме
    9 000 рублей» сумма относится к услугам, а не к тому, что стоит
    в пункте дальше. Если якоря перед суммой нет, берётся ближайший
    после: в записи «в размере 18 390 рублей в качестве неустойки»
    основание стоит именно так. При равной позиции выигрывает более
    длинный (то есть более конкретный) якорь: «компенсация морального
    вреда» вместо общего «компенсация».
    """
    span = _first_amount_span(clause)
    limit = span[0] if span is not None else len(clause)
    before: tuple[int, int, int] | None = None
    after: tuple[int, int, int] | None = None
    for order, (pattern, _basis) in enumerate(CLAIM_BASIS_ANCHORS):
        for match in pattern.finditer(clause):
            candidate = (match.start(), match.end() - match.start(), order)
            if match.start() <= limit:
                if before is None or candidate[0] > before[0] or (
                    candidate[0] == before[0] and candidate[1] > before[1]
                ):
                    before = candidate
            elif after is None or candidate[0] < after[0]:
                after = candidate
    best = before if before is not None else after
    if best is None:
        return None
    return CLAIM_BASIS_ANCHORS[best[2]][1]


class _ClauseResult(NamedTuple):
    """Результат разбора окна по пунктам перечисления.

    claims — требования с суммой: (основание, рубли, копейки);
    blanks — основания, у которых сумма не нашлась (требование без
    суммы: оно должно остаться видимым, а не исчезнуть);
    conflicts — по одному основанию две разные суммы: (основание,
    рубли и копейки первой, рубли и копейки второй).
    """

    claims: list[tuple[str, int, int]]
    blanks: list[str]
    conflicts: list[tuple[str, int, int, int, int]]


def _clause_claims(text: str) -> _ClauseResult:
    """Требования по пунктам перечисления.

    Окно требования часто перечисляет несколько пунктов: «стоимость
    товара в размере 10 490 рублей, неустойку за просрочку в размере
    2 202 рубля 90 копеек, компенсацию морального вреда в размере
    10 000 рублей». Разбор окна целиком даёт одно основание, и
    остальные суммы терялись (real-03 — неустойка 18 390, real-04 —
    стоимость товара 10 490 и услуги представителя 7 000).

    Разбирается только ПЕРВОЕ предложение окна: дальше идёт
    мотивировка суда, и сумма оттуда попадала бы в проект как
    требование («судом установлено, что ущерб составил 7777 рублей»).
    Пункты с признаками обстоятельств пропускаются.

    Основание берётся самое конкретное (порядок якорей), сумма — первая
    в пункте. Пункт с пошлиной пропускается: пошлина выносится
    отдельно по ст. 98 ГПК РФ.
    """
    if not text:
        return _ClauseResult([], [], [])
    sentence = _SENTENCE_SPLIT.split(text)[0]
    claims: list[tuple[str, int, int]] = []
    blanks: list[str] = []
    conflicts: list[tuple[str, int, int, int, int]] = []
    seen: dict[str, tuple[int, int] | None] = {}
    clauses = _clauses(sentence)
    index = 0
    while index < len(clauses):
        clause = clauses[index]
        if _is_fee_requirement(clause) or _NARRATION_MARKERS.search(clause):
            index += 1
            continue
        basis = _clause_basis(clause)
        if basis is None:
            index += 1
            continue
        found = _find_amount_with_kopecks(clause)
        consumed = 0
        if found is None:
            # Основание отдельно от суммы: «убытки, связанные с
            # получением кредита в сумме 3000 рублей». Пункт
            # соединяется только со следующим и только если у того
            # нет своего основания — иначе сумма досталась бы чужому
            # пункту («убытки, неустойку в сумме 2202 рублей»), а
            # через два пункта сумма приклеивалась совсем издалека
            # («штраф в пользу потребителя, расходы на юридические
            # услуги, … в сумме 9000 рублей»).
            if index + 1 < len(clauses):
                following = clauses[index + 1]
                if (
                    not _is_fee_requirement(following)
                    and not _NARRATION_MARKERS.search(following)
                    and _clause_basis(following) is None
                ):
                    found = _find_amount_with_kopecks(following)
                    if found is not None:
                        consumed = 1
            if found is None:
                if basis not in seen:
                    seen[basis] = None
                    blanks.append(basis)
                index += 1
                continue
        index += consumed + 1
        amount = found[0]
        kopecks = found[1]
        if basis in seen:
            previous = seen[basis]
            if previous is not None and previous != (amount, kopecks):
                conflicts.append((basis, previous[0], previous[1], amount, kopecks))
            continue
        seen[basis] = (amount, kopecks)
        claims.append((basis, amount, kopecks))
    return _ClauseResult(claims, blanks, conflicts)


def extract_parties(pack: EvidencePack) -> Parties:
    """Названия сторон для резолютивной части.

    Разбор 1 425 гражданских решений показал: суд называет стороны
    конкретно — ФИО (277 документов) или организацией (403) — чаще,
    чем родовыми словами (358). Поэтому из пакета берутся настоящие
    названия, а «истец» и «ответчик» остаются запасным вариантом на
    случай, когда участник не установлен.

    Название должно быть в родительном падеже, потому что входит в
    конструкцию «взыскать с …». Привести имя к падежу автоматически
    по-настоящему нельзя, поэтому берётся форма, уже стоящая в
    материалах рядом со словом «Ответчик:», иначе — то, что после
    слова «Истец:». Если в материалах только имя в именительном,
    оно используется как есть: ошибка падежа в резолюции заметнее,
    чем родовое слово.
    """
    plaintiff = ""
    defendant = ""
    for participant in pack.participants:
        text = participant.strip()
        match = re.match(r"^(.*?)\s*\((истец|ответчик)\)\s*$", text, re.I)
        if not match:
            continue
        name, role = match.group(1).strip(), match.group(2).lower()
        if role == "истец" and not plaintiff:
            plaintiff = name
        elif role == "ответчик" and not defendant:
            defendant = name

    return Parties(plaintiff=plaintiff, defendant=defendant)


def extract_fee_and_costs(pack: EvidencePack) -> tuple[int | None, int | None]:
    """Размер пошлины и иных расходов по ст. 98 и 100 ГПК РФ."""
    return (
        _fee_from(pack.items),
        _costs_from(pack.by_category("claims") + pack.by_category("evidence")),
    )
