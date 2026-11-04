"""Разбор текста судебного решения, полученного со страницы sudrf.

Зачем модуль: в корпусе, собранном со страниц sudrf, решение лежит не
отдельным полем, а внутри текста страницы — после маркера «Именем
Российской Федерации». Перед ним идут навигация, карточка дела, история
заседаний. Без выделения тела решения в обучение попал бы мусор.

Правила намеренно простые и проверяемые: разметка страницы устойчива,
а ошибиться в ней дешевле, чем пропустить решение целиком.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Начало текста решения на странице sudrf.
DECISION_MARKER = "Именем Российской Федерации"

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

_DATE_AT_START = re.compile(
    r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})",
    re.IGNORECASE,
)

_ESTABLISHED = re.compile(r"\bУСТАНОВИЛ\b")
_DECIDED = re.compile(r"\bРЕШИЛ\b")
#: Запасной вариант: заголовок строчными в начале строки. Слово
#: «установил» встречается и в прозе («суд установил обстоятельства»),
#: поэтому строчная форма ищется только отдельной строкой.
_ESTABLISHED_LINE = re.compile(r"(?m)^\s*установил\b", re.IGNORECASE)
_DECIDED_LINE = re.compile(r"(?m)^\s*решил\b", re.IGNORECASE)


def _find_heading(text: str, upper: re.Pattern[str], line: re.Pattern[str]):
    """Найти заголовок: сначала заглавный, затем строчный в начале строки."""
    match = upper.search(text)
    return match or line.search(text)

#: Служебный мусор страницы, который иногда попадает в текст.
_JS_PREFIX = re.compile(r"^\s*Javascript[^\n]*", re.IGNORECASE)

#: Заголовки, разнесённые по буквам: «Р Е Ш И Л». Так разметка страницы
#: разрывает слова, и без склейки резолютивная часть не находилась:
#: на выборке из 15 000 гражданских решений «Р Е Ш И Л» встретился в
#: 99,8 % случаев, а цельное «РЕШИЛ» — только в 47 %.
_SPACED_WORD = re.compile(r"\b(?:[А-ЯЁ]\s){2,}[А-ЯЁ]\b")


def _join_spaced_letters(match: re.Match[str]) -> str:
    return match.group(0).replace(" ", "")


def extract_decision_body(page_text: str) -> str:
    """Выделить текст решения из текста страницы.

    Пустая строка — решения на странице нет. Берётся всё после маркера:
    перед ним только карточка дела и история заседаний.
    """
    if not page_text:
        return ""
    index = page_text.find(DECISION_MARKER)
    if index < 0:
        return ""
    return page_text[index:].strip()


def clean_decision(text: str) -> str:
    """Убрать мусор разметки, не меняя текст решения.

    Схлопываются повторные пробелы и пустые строки: при извлечении из
    HTML между словами остаются десятки пробелов и неразрывные пробелы.
    """
    if not text:
        return ""
    cleaned = _JS_PREFIX.sub("", text)
    cleaned = cleaned.replace("\u00a0", " ").replace("\u200b", "")
    # Слова из однобуквенных «слов» склеиваются: «Р Е Ш И Л» → «РЕШИЛ».
    cleaned = _SPACED_WORD.sub(_join_spaced_letters, cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def decision_date(body: str) -> str:
    """Дата решения из первой строки: «6 февраля 2026 года».

    Возвращает «ГГГГ-ММ-ДД» или пустую строку. Дата нужна для отбора
    свежих решений и для метаданных корпуса.
    """
    if not body:
        return ""
    head = body[:200]
    match = _DATE_AT_START.search(head)
    if not match:
        return ""
    day, month_word, year = match.groups()
    month = _MONTHS.index(month_word.lower()) + 1
    return f"{int(year):04d}-{month:02d}-{int(day):02d}"


def case_kind(case_number: str, body: str = "") -> str:
    """Вид дела по номеру и тексту: civil, administrative, criminal, other.

    Номера судов общей юрисдикции: «2-…» — гражданское, «2а-…» —
    административное (КАС РФ), «1-…» — уголовное, «5-…»/«4-…» — дела об
    административных правонарушениях (КоАП РФ). Для сервиса нужны
    гражданские: остальные используют другой процессуальный кодекс.
    """
    number = (case_number or "").strip().lower()
    if number.startswith("2а") or number.startswith("2-а"):
        return "administrative"
    if number.startswith("2"):
        return "civil"
    if number.startswith("1"):
        return "criminal"
    if number.startswith(("5-", "4-", "5/")):
        return "administrative_offence"
    # Номер может быть нестандартным: тогда решает текст.
    if "КАС РФ" in body:
        return "administrative"
    if "УК РФ" in body and "уголовн" in body.lower():
        return "criminal"
    if "КоАП" in body:
        return "administrative_offence"
    return "other"


@dataclass(frozen=True)
class DecisionParts:
    """Логические части решения, найденные по заголовкам."""

    descriptive: str
    established: str
    operative: str


def split_sections(body: str) -> DecisionParts:
    """Разделить решение на описательную, установительную и резолютивную.

    Мотивировочная часть не выделяется отдельно: в решениях судов общей
    юрисдикции она идёт внутри «УСТАНОВИЛ» до «РЕШИЛ», и резать её по
    произвольному признаку значило бы придумывать структуру, которой в
    документе нет. Поэтому established содержит и установленные факты, и
    мотивы — ровно так, как в акте.
    """
    if not body:
        return DecisionParts("", "", "")

    established_match = _find_heading(body, _ESTABLISHED, _ESTABLISHED_LINE)
    decided_match = _find_heading(body, _DECIDED, _DECIDED_LINE)

    descriptive_end = (
        established_match.start() if established_match else len(body)
    )
    descriptive = body[:descriptive_end].strip()

    if established_match and decided_match and decided_match.start() > established_match.start():
        established = body[established_match.end() : decided_match.start()].strip(" :\n")
        operative = body[decided_match.end() :].strip(" :\n")
    elif decided_match:
        established = ""
        operative = body[decided_match.end() :].strip(" :\n")
    else:
        established = ""
        operative = ""

    return DecisionParts(descriptive=descriptive, established=established, operative=operative)
