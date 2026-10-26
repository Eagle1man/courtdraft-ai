"""Родительный падеж для наименований сторон.

В резолюции стороны стоят в родительном падеже: «взыскать с Образцова
Петра Петровича в пользу Образцова». Без склонения получается «взыскать
с Образцов Пётр Петрович» — ошибка прямо в формуле постановления.

Склонение сделано консервативно и только там, где результат известен
наверняка:

* юридическое лицо не склоняется вовсе — «с ООО «Фирма Строй»» верно;
* человек склоняется, только если склоняются все три части ФИО и
  окончания попали в белый список;
* во всех остальных случаях название возвращается как есть, а вызывающий
  добавляет видимую пометку о необходимости проверить падеж.

Гадать падеж нельзя: ошибка в резолютивной части не исправляется
судьёй постфактум, а обнаружить её проще всего — и обнаруживают.
"""

from __future__ import annotations

import re

#: Организационно-правовые формы. Такое название не склоняется:
#: «взыскать с ООО «Ромашка»», а не «с ООО «Ромашки»».
ORG_FORM = re.compile(
    r"^\s*(?:ООО|ОАО|ЗАО|ПАО|АО|ИП|ГУП|МУП|ФГУП|ФНС|НКО|Фонд|ТОО|КОО|"
    r"ГК\s+УК|ГУ\s+МВД|МИФНС)\b",
    re.IGNORECASE,
)

#: Фамилия в кавычках — уже в форме организации.
QUOTED = re.compile(r"[«\"“]")

PATRONYMIC = re.compile(r"^(.+)\s+(.+)\s+(.+)$")

#: Отчество опознаётся по окончанию. Раньше проверялся строгий список
#: («ович», «евич», «ична»), и распространённые «Никитич», «Ильич» в
#: человека не узнавали.
#:
#: Живое дело (1w121): «Викторовна» не опознавалась — покрытие было
#: только «ич(на)», а женские отчества оканчиваются на «овна» и
#: «евна». Фамилия в том же виде («Смирнова») сюда не попадает: там
#: «ова», а не «овна».
PATRONYMIC_TAIL = re.compile(r"(?:овн|евн|ичн|ич)$", re.IGNORECASE)

#: Окончания, при которых склонение известно.
#:
#: Правило — тройка (окончания, действие, новое окончание). Действие
#: «append» ПРИСТАВЛЯЕТ окончание: «Пётр» → «Пётра», «Путин» →
#: «Путина», «Иванов» → «Иванова». Действие «replace» ЗАМЕНЯЕТ
#: найденное окончание: «Андрей» → «Андрея», «Игорь» → «Игоря».
#:
#: Разница принципиальна. Смешав её в одной регулярке, «Пётр»
#: превращалось в «Пёта», а «Образцов» — в «Образцоа»: буква терялась
#: вместо того, чтобы окончание добавлялось.
_CONSONANTS = "бвгджзклмнпрстфхцшщъ"
_NAME_RULES: tuple[tuple[tuple[str, ...] | None, str, str], ...] = (
    (("й", "ь"), "replace", "я"),
    (None, "append", "а"),
)
_SURNAME_RULES: tuple[tuple[tuple[str, ...] | None, str, str], ...] = (
    (("ин", "ын", "ов", "ев", "ёв"), "append", "а"),
    (None, "append", "а"),
)


def _apply_rules(
    token: str, rules: tuple[tuple[tuple[str, ...] | None, str, str], ...]
) -> str | None:
    """Согласовать окончание, либо вернуть None, если это не известно."""
    if not token:
        return None
    lowered = token.lower()
    for suffixes, action, ending in rules:
        if suffixes is None:
            if token[-1].lower() in _CONSONANTS:
                return token + ending
            continue
        for suffix in suffixes:
            if lowered.endswith(suffix):
                if action == "replace":
                    return token[: -len(suffix)] + ending
                return token + ending
    return None


def is_person(name: str) -> bool:
    """Похоже ли название на ФИО человека."""
    if not name.strip() or ORG_FORM.match(name) or QUOTED.search(name):
        return False
    parts = name.split()
    if len(parts) < 2:
        return False
    return PATRONYMIC_TAIL.search(parts[-1]) is not None


def can_decline(name: str) -> bool:
    """Можно ли уверенно привести название к родительному падежу."""
    if not name.strip():
        return False
    if ORG_FORM.match(name) or QUOTED.search(name):
        # Юридическое лицо не склоняется, падеж здесь не требуется.
        return True
    match = PATRONYMIC.match(name.strip())
    if not match:
        return False
    surname, given, patronymic = match.groups()
    if not PATRONYMIC_TAIL.search(patronymic):
        return False
    return (
        _apply_rules(surname, _SURNAME_RULES) is not None
        and _apply_rules(given, _NAME_RULES) is not None
    )


def to_genitive(name: str) -> str:
    """Название стороны в родительном падеже, если это известно.

    Юридическое лицо возвращается без изменений: склонять его нельзя.
    ФИО склоняется по всем трём частям сразу — склонённая частично
    фраза хуже, чем несклонённая целиком.
    """
    text = name.strip()
    if not text:
        return text
    if ORG_FORM.match(text) or QUOTED.search(text):
        return text
    match = PATRONYMIC.match(text)
    if not match:
        return text
    surname, given, patronymic = match.groups()

    declined_surname = _apply_rules(surname, _SURNAME_RULES)
    declined_given = _apply_rules(given, _NAME_RULES)
    if declined_surname is None or declined_given is None:
        return text

    if patronymic.lower().endswith("ична"):
        declined_patronymic = patronymic[: -len("ична")] + "ичны"
    elif patronymic.lower().endswith("ич"):
        declined_patronymic = patronymic + "а"
    else:
        return text

    return f"{declined_surname} {declined_given} {declined_patronymic}"
