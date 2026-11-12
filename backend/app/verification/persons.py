"""Проверка персон и ролей сторон (Фаза 10).

Зачем отдельный модуль: числа и ссылки на нормы проверяются регулярками,
а персоны требуют сопоставления с материалами дела, и правил тут больше,
чем помещается в основной движок.

Что ловится:

* **выдуманные ФИО.** Модель написала «Судья: Иванов Иван Иванович» —
  такого человека в материалах нет. Прежняя проверка это пропускала:
  она сверяла числа и нормы, а персон не трогала.
* **выдуманный состав суда.** «Состав суда: прокурор, судья,
  секретарь» — в деле нет ни такого состава, ни этих лиц.
* **перепутанные роли сторон.** «Истец обязуется уплатить» вместо
  «суд взыскивает с истца в пользу ответчика» — самый опасный вид
  ошибки, потому что формально опирается на материалы.

ГЛАВНОЕ ОГРАНИЧЕНИЕ. Модуль помечает подозрение, а не доказывает
выдумку. Любая находка требует внимания человека: ложное срабатывание
здесь опаснее пропуска, потому что поданная судьё претензия может
заблокировать нормальный текст. Поэтому проверки настроены строго:
работают только на бесспорных признаках — полное ФИО и явное перечисление
состава суда.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.agents.evidence_pack import EvidencePack

# ── Признаки выдуманной персоны ──────────────────────────────────────

# Полное ФИО: три слова с заглавной буквы. Короткое «Иванов» или
# «Иван» пропускаем: такая форма встречается и в пересказе, и в ссылке.
FIO_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,15})\s+([А-ЯЁ][а-яё]{2,15})\s+([А-ЯЁ][а-яё]{2,20})\b"
)

# ФИО с инициалами: «Иванов И. И.», «Петров П.П.»
FIO_SHORT_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,15})\s+([А-ЯЁ])\.\s*([А-ЯЁ])\."
)

# Слова, из-за которых регулярка ФИО склеивает обычную фразу в «персону».
# «Истец Образцов Пётр» — это роль плюс фамилия, а не выдуманное ФИО.
# Без этого списка каждая фраза вида «Ответчик Фирма «...»» считалась бы
# персоной, и проверка тонула бы в ложных срабатываниях.
ROLE_WORDS = frozenset(
    {
        "истец", "истца", "истцу", "истцом", "истце", "истцы", "истцов",
        "ответчик", "ответчика", "ответчику", "ответчиком", "ответчике",
        "ответчики", "ответчиков",
        "третье", "третьего", "третьему", "третьим",
        "лицо", "заявитель", "заявителя", "заявителю",
        "представитель", "представителя", "представителю",
        "должник", "должника", "должнику", "кредитор", "кредитора",
        "продавец", "продавца", "покупатель", "покупателя",
        "заказчик", "заказчика", "исполнитель", "исполнителя",
        "подрядчик", "подрядчика", "суд", "судья", "судьи", "секретарь", "секретаря",
        "прокурор", "прокур��ра", "помощник",
        "свидетель", "свидетеля", "эксперт", "эксперта",
        "взыскатель", "взыскателя",
    }
)

# Перечисление состава суда: «Состав суда: прокурор, судья, секретарь»,
# «судьи: Иванов И.И., Петрова А.А.». Проверяем именно формулировки
# про состав суда, а не любое упоминание слова «судья» — иначе под правило
# попали бы нормальные фразы вида «судья рассмотрел дело».
#
# Точка внутри перечисления разрешена: иначе перечисление обрывалось бы
# на первой инициале («Судьи: Иванов И. И.» читалось как «Иванов И»).
# Граница — только «;» и конец строки. Различать точку инициала и точку
# конца предложения регулярным выражением нельзя: в «И. И.» точка стоит
# перед пробелом так же, как в конце фразы. Лишние слова из следующей
# фразы безвредны — из них извлекаются те же персоны и сверяются
# с материалами.
COMPOSITION_RE = re.compile(
    r"(?:состав\s+суда|судьи|состав\s+судебного\s+состава|суд\s+в\s+составе)"
    r"\s*[:—-]?\s*([^\n;]{3,250})",
    re.IGNORECASE,
)

# Должности, которые не должны появляться в составе суда по гражданскому
# делу в органах судопроизводства. Прокурор в составе суда — верный признак
# того, что модель сочинила текст: прокурор не входит в состав суда.
IMPOSSIBLE_IN_COMPOSITION = ("прокурор", "следователь", "адвокат", "помощник")

# ── Роли сторон ──────────────────────────────────────────────────────

PLAINTIFF = "истец"
DEFENDANT = "ответчик"

# Падежные формы ролей. Без них проверка молчала: в резолютивной части
# сторон называют по падежам («с истца», «истцу должен»), и поиск по
# именительному «истец» ничего не находил.
PLAINTIFF_FORMS = frozenset(
    {"истец", "истца", "истцу", "истцом", "истце", "истцы", "истцов"}
)

# Формы, в которых истец может быть плательщиком. Отбрасывать дательный
# нельзя: «Истцу надлежит уплатить» называет истца плательщиком.
# Различает получателя и плательщика не падеж, а позиция глагола —
# см. check_roles.
PLAINTIFF_AS_SUBJECT = frozenset(PLAINTIFF_FORMS)

# Пассивная форма: «Истцом взыскано 150000 рублей».
PLAINTIFF_PASSIVE = frozenset({"истцом"})
DEFENDANT_FORMS = frozenset(
    {"ответчик", "ответчика", "ответчику", "ответчиком", "ответчике",
     "ответчики", "ответчиков"}
)

# Глаголы, после которых участник выступает плательщиком.
# Связки «должен/должна» сюда НЕ входят: они часто не про платёж, а про
# принадлежность («с истца задолженность»), и давали ложные срабатывания.
# Формулировки вида «истец должен уплатить» всё равно ловятся по «уплатить».
PAY_VERBS = (
    "обязуется", "обязан", "обязуются", "обязаны",
    "уплатить", "уплачивает", "выплатить", "выплачивает",
    "перечислить", "перечисляет", "внести", "вносит",
    "оплатить", "оплачивает", "погасить", "возмести",
    "взыскано", "взыскивается", "удержано",
)

# Оборот предписания взыскания: «взыскать с истца», «взыскивает с ответчика».
COLLECT_FROM_RE = re.compile(
    r"(?:взыскать|взыскивает|взыскано|удержать|удерживается)\s+с\s+"
    r"([а-яё]{3,20})",
    re.IGNORECASE,
)

# Безличные и пассивные формы, из которых ясно, что взыскание идёт
# с лица плательщика: «Истцом взыскано 150000 рублей».
PASSIVE_PAY_RE = re.compile(
    rf"(?:{'|'.join(PAY_VERBS)})\s+(?:с\s+)?([а-яё]{{3,20}})", re.IGNORECASE
)

# Упоминание нормы: в таком фрагменте роли разбирать нельзя, там
# перечисляются стороны как участники цитаты, а не как плательщики.
LAW_MENTION_RE = re.compile(
    r"(?:ст\.|статья|статьи|статье|статью|пункт\s+\d)", re.IGNORECASE
)


@dataclass(frozen=True)
class PersonFinding:
    """Подозрение на выдуманную персону."""

    person: str
    section: str
    reason: str


@dataclass(frozen=True)
class RoleFinding:
    """Подозрение на переставленные роли сторон."""

    detail: str
    section: str


def _normalize(value: str) -> str:
    """Привести ФИО к сравнимому виду: без точек, лишних пробелов."""
    return re.sub(r"\s+", " ", value.replace(".", " ")).strip().lower()


def _material_persons(pack: EvidencePack) -> set[str]:
    """Персоны, реально встречающиеся в материалах дела.

    Собираются из текста всех фактов пакета и из участников дела.
    Сравнение идёт по нормализованному виду, чтобы «Иванов Иван Иванович»
    и «Иванов И. И.» считались одним человеком.
    """
    found: set[str] = set()

    for entry in pack.participants:
        # «Образцов Пётр Петрович (истец)» -> «Образцов Пётр Петрович»
        name = re.sub(r"\s*\([^)]*\)\s*$", "", entry).strip()
        if name:
            found.add(_normalize(name))

    blob = "\n".join(item.text for item in pack.items)
    blob += "\n" + "\n".join(pack.participants)

    for match in FIO_RE.finditer(blob):
        found.add(_normalize(" ".join(match.groups())))
    for match in FIO_SHORT_RE.finditer(blob):
        found.add(_normalize(f"{match.group(1)} {match.group(2)} {match.group(3)}"))
    for surname, name, _ in FIO_RE.findall(blob):
        found.add(_normalize(surname))
        found.add(_normalize(name))

    return found


def _section_persons(text: str) -> list[str]:
    """Персоны, упомянутые в тексте раздела.

    Совпадения, где хоть одно слово — обозначение роли, отбрасываются:
    «Истец Образцов Пётр» это роль и фамилия, а не ФИО из трёх слов.
    """
    persons: list[str] = []

    def _usable(words: tuple[str, ...]) -> bool:
        lowered = [w.lower() for w in words]
        # Повтор полного слова («Образцов Образцов») — не ФИО.
        # Инициалы в проверку не входят: у «Петров П. П.» оба инициала
        # одинаковы по определению, и такая проверка отбрасывала бы
        # половину корректных ФИО с инициалами.
        full_words = [w for w in lowered if len(w) > 1]
        if len(set(full_words)) < len(full_words):
            return False
        return not any(word in ROLE_WORDS for word in lowered)

    for match in FIO_RE.finditer(text):
        words = match.groups()
        if _usable(words):
            persons.append(" ".join(words))
    for match in FIO_SHORT_RE.finditer(text):
        words = (match.group(1), match.group(2), match.group(3))
        if _usable(words):
            persons.append(f"{words[0]} {words[1]} {words[2]}")

    return persons


def check_persons(text: str, section: str, pack: EvidencePack) -> list[PersonFinding]:
    """Найти ФИО, которых нет в материалах дела."""
    known = _material_persons(pack)
    findings: list[PersonFinding] = []
    seen: set[str] = set()

    for person in _section_persons(text):
        key = _normalize(person)
        if not key or key in seen:
            continue
        seen.add(key)

        surname = person.split()[0]
        # Совпадение по фамилии засчитываем: в материалах может быть
        # «Иванов Иван», а модель написала «Иванов Иван Иванович».
        if key in known or _normalize(surname) in known:
            continue

        findings.append(
            PersonFinding(
                person=person,
                section=section,
                reason=(
                    f"персона «{person}» не встречается в материалах дела — "
                    "возможно выдумана моделью"
                ),
            )
        )

    return findings


def check_composition(text: str, section: str, pack: EvidencePack) -> list[PersonFinding]:
    """Проверить достоверность состава суда.

    Две проверки: в составе не должно быть лиц, которых нет в деле, и
    в нём не должно быть прокурора — прокурор не входит в состав суда,
    и его появление означает, что текст сочинён.
    """
    known = _material_persons(pack)
    findings: list[PersonFinding] = []

    for match in COMPOSITION_RE.finditer(text):
        listing = match.group(1).strip()

        lowered = listing.lower()
        for word in IMPOSSIBLE_IN_COMPOSITION:
            if word in lowered:
                findings.append(
                    PersonFinding(
                        person=listing,
                        section=section,
                        reason=(
                            f"в составе суда указан «{word}», который не входит "
                            "в состав суда — вероятно, текст сочинён"
                        ),
                    )
                )

        # Упомянутые в перечне лица должны быть знакомы по материалам.
        for person in _section_persons(listing):
            surname = person.split()[0]
            if _normalize(person) in known or _normalize(surname) in known:
                continue
            findings.append(
                PersonFinding(
                    person=person,
                    section=section,
                    reason=(
                        f"в составе суда указан «{person}», которого нет "
                        "в материалах дела"
                    ),
                )
            )

    return findings


def _role_by_name(pack: EvidencePack) -> dict[str, str]:
    """Сопоставить участникам дела их роли.

    Ключ — нормализованные слова ФИО. В резолютивной части сторону
    называют как словом «истец», так и по имени («Образцов обязуется
    уплатить»), поэтому роль приходится искать обоими способами.
    """
    mapping: dict[str, str] = {}

    for entry in pack.participants:
        match = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", entry)
        if not match:
            continue
        name, role = match.group(1).strip(), match.group(2).strip().lower()
        normalized = _normalize(name)
        if not normalized:
            continue

        mapping[normalized] = role
        # Отдельными ключами — по фамилии и по имени: в тексте может
        # остаться только «Образцов».
        for word in normalized.split():
            if len(word) > 3:
                mapping.setdefault(word, role)

    return mapping


def _plaintiff_mentions(sentence: str, roles_by_name: dict[str, str]) -> list[tuple[int, str]]:
    """Где в предложении истец выступает субъектом действия.

    Возвращает позиции и то, как он назван: словом «истец» или по имени.
    Плательщиком истец считается только когда он стоит перед платёжным
    глаголом — сама позиция слова ещё ничего не говорит.

    Формы берутся все: дательный «Истцу надлежит уплатить» означает
    плательщика, а «возместить истцу» — получателя. Различает их как раз
    позиция глагола, а не падеж.
    """
    found: list[tuple[int, str]] = []
    lowered = sentence.lower()

    for form in sorted(PLAINTIFF_FORMS, key=len, reverse=True):
        for match in re.finditer(rf"\b{form}\b", lowered):
            found.append((match.start(), form))

    for name, role in roles_by_name.items():
        if role != PLAINTIFF:
            continue
        for match in re.finditer(rf"\b{re.escape(name)}\b", lowered):
            found.append((match.start(), name))

    return sorted(set(found))


def check_roles(text: str, section: str, pack: EvidencePack) -> list[RoleFinding]:
    """Найти переставленные роли сторон.

    Ловится самая опасная ошибка: сторона, которая должна получить
    деньги, поставлена плательщиком. В реальном прогоне модель писала
    «Образцов Петр Петрович обязуется … оплатить сумму 150000 рублей» —
    то есть истец платит вместо ответчика.

    Истец ищется и словом («истец обязуется»), и по имени («Образцов
    обязуется»): в резолютивной части встречаются обе формы.

    Проверка идёт по предложениям. Фрагмент с упоминанием нормы
    пропускается: там стороны перечисляются как участники цитаты,
    а разбирать их роли нельзя.
    """
    findings: list[RoleFinding] = []
    seen: set[str] = set()
    roles_by_name = _role_by_name(pack)
    verbs = "|".join(PAY_VERBS)

    for sentence in re.split(r"(?<=[.;])\s+|\n+", text):
        if not sentence.strip():
            continue
        if LAW_MENTION_RE.search(sentence):
            continue

        if LAW_MENTION_RE.search(sentence):
            continue

        # 1. Истец назван плательщиком.
        # Замечание одно на предложение: иначе «Образцов Пётр
        # Петрович обязуется» дал бы три находки по фамилии, имени
        # и отчеству, и список замечаний засорялся бы дублями одного
        # и того же дефекта.
        mentions = _plaintiff_mentions(sentence, roles_by_name)
        if mentions:
            verb_matches = list(
                re.finditer(rf"\b(?:{verbs})\b", sentence, re.IGNORECASE)
            )
            first_verb = verb_matches[0].start() if verb_matches else None
            first_verb_word = (
                verb_matches[0].group(0).lower() if verb_matches else ""
            )

            position, mention = mentions[0]

            # Истец — плательщик, только если он стоит ДО платёжного
            # глагола. В «ответчик обязан возместить истцу сумму» глагол
            # стоит раньше истца, истец здесь получатель, а не плательщик.
            # На живом прогоне прежняя версия правила помечала такую
            # правильную фразу как CRITICAL.
            if first_verb is not None and position < first_verb:
                between = sentence[position:first_verb].lower()
                has_defendant_between = any(
                    re.search(rf"\b{form}\b", between) for form in DEFENDANT_FORMS
                )
                # «Истцом удержано/взыскано» — действие взыскателя (удержал,
                # взыскал), а не истец-плательщик.
                passive_agent = (
                    mention.lower() in PLAINTIFF_PASSIVE
                    and first_verb_word in {"удержано", "взыскано", "взыскивается"}
                )
                if not has_defendant_between and not passive_agent:
                    fragment = f"{sentence[:90].strip()}"
                    if fragment not in seen:
                        seen.add(fragment)
                        findings.append(
                            RoleFinding(
                                detail=(
                                    f"истец указан плательщиком: «{fragment}». "
                                    "По делу истец взыскатель."
                                ),
                                section=section,
                            )
                        )

        # 2. Взыскание предписано с истца.
        for match in COLLECT_FROM_RE.finditer(sentence):
            target = match.group(1).lower()
            if target not in PLAINTIFF_FORMS:
                # С ответчика взыскание законно и ожидаемо.
                continue
            fragment = match.group(0)[:100]
            if fragment in seen:
                continue
            seen.add(fragment)
            findings.append(
                RoleFinding(
                    detail=(
                        f"взыскание предписано с истца («{fragment}») — "
                        "по материалам дела истец выступает взыскателем"
                    ),
                    section=section,
                )
            )

    return findings


def check_section(
    text: str, section: str, pack: EvidencePack
) -> tuple[list[PersonFinding], list[RoleFinding]]:
    """Проверить раздел целиком.

    Возвращает отдельно персон и роли, чтобы вызывающий код мог присвоить
    им разные типы замечаний и разную серьёзность.
    """
    persons = check_persons(text, section, pack)
    persons += check_composition(text, section, pack)
    roles = check_roles(text, section, pack)
    return persons, roles


def known_persons(pack: EvidencePack) -> Iterable[str]:
    """Список персон из материалов — для показа в интерфейсе."""
    return sorted(_material_persons(pack))
