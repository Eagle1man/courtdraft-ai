"""Номер дела и суд — из самих материалов, а не из карточки.

Зачем: карточку заполнял человек, и в поле «кто рассматривает» попадало что
угодно — фамилия, «уц», адрес. Проект при этом брал суд из документов, и
проверка справедливо ругалась на противоречие (18 замечаний за тест).

Правильное место для этих данных — материалы дела: номер и суд там написаны
в шапке иска, определения или протокола. Поэтому значения извлекаются после
загрузки документа и подставляются в карточку.

Извлечение намеренно простое и объяснимое: несколько шаблонов, без модели.
Ошибка здесь дешевле пропуска: не нашли — в карточке остаётся «по материалам»,
и проект всё равно берёт реквизиты из документов.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Название суда стоит отдельной строкой шапки. Берём строку целиком, а не
# «до первой точки»: в названиях есть сокращения — «г. Москвы», — и обрезка
# по точке превращала «Мировой судья ... г. Москвы» в «... Хамовники г».
_COURT_LINE = re.compile(
    r"("
    r"Арбитражн\w*\s+суд"
    r"|Мировой\s+судья"
    r"|Судебн\w+\s+участок"
    r"|Верховн\w+\s+суд"
    r"|[А-ЯЁ][а-яё\-]+\s+(?:районн\w*|городск\w*|областн\w*|краев\w*|"
    r"республиканск\w*)\s+суд"
    r")",
    re.IGNORECASE,
)

# Номер дела: «Дело № 2-456/2025», «№ А40-12345/2026», «дело № 121».
_NUMBER_PATTERNS = (
    re.compile(
        r"(?:гражданск\w+\s+дело|дело)\s*№\s*"
        r"([0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё/\-–—]{1,30})",
        re.IGNORECASE,
    ),
    re.compile(r"№\s*([0-9]{1,3}\s*-\s*[0-9]{1,6}\s*/\s*[0-9]{4})"),
    re.compile(r"\b([АA][0-9]{1,2}-[0-9]{1,7}/[0-9]{4})\b"),
)

_TRAILING = " .,;:—-–—"


def _clean_court(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(_TRAILING)
    # Предлог в начале адресного блока: «В Арбитражный суд ...».
    text = re.sub(r"^[Вв]\s+(?=[А-ЯЁ])", "", text)
    # Почтовый индекс в конце шапки судом не является.
    text = re.sub(r"\s+\d{6}$", "", text).strip(_TRAILING)
    return text[:200]


def _clean_number(value: str) -> str:
    text = re.sub(r"\s+", "", value).strip(_TRAILING)
    # «2-456/2025» и «2 - 456 / 2025» — один и тот же номер.
    text = text.replace("–", "-").replace("—", "-")
    return text[:100]


def _find_court(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _COURT_LINE.search(line)
        if match:
            # Берём строку с самого названия суда: перед ним бывает
            # «Ходатайство.» или «В» — это не часть названия.
            return _clean_court(line[match.start():])
    return ""


def _find_number(text: str) -> str:
    for pattern in _NUMBER_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1) if match.groups() else match.group(0)
            return _clean_number(raw)
    return ""


def extract_case_card(texts: Iterable[str]) -> tuple[str, str]:
    """Вернуть (номер дела, суд) из текстов материалов.

    Пустая строка — данных нет: вызывающий решает, оставить ли прежнее
    значение. Ищем по всем документам, пока не найдём оба.
    """
    number = ""
    court = ""
    for text in texts:
        if not text:
            continue
        if not number:
            number = _find_number(text)
        if not court:
            court = _find_court(text)
        if number and court:
            break
    return number, court
