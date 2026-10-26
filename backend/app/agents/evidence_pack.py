"""Пакет доказательств (Фаза 8, ТЗ п.26).

Evidence Pack — то, из чего Draft Agent строит проект. Смысл в том,
чтобы модель НЕ придумывала: каждый блок пакета содержит факты со
ссылкой на источник, а система проверки потом сверяет текст проекта
с этим пакетом.

Категории соответствуют `evidence_categories` шаблона решения.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.agents.claim_phrases import looks_like_claim
from app.config import Settings, get_settings
from app.models.entities import CaseDocument

# Ключевые суммы дела: извлекаются регулярками, а не поручаются модели.
# Модель на 1.5B параметров не умеет надёжно считать и путает цифры —
# значит, числа в проект попадают только из материалов.
MONEY_RE = re.compile(
    # Сумма с необязательными копейками: «120 000», «120 000,00», «150000».
    # Копейки не часть суммы: «120 000,00 руб.» — это 120000, а не 00.
    r"(\d{1,3}(?:[\d  ]\d{3})+|\d+)(?:,\d{1,2})?\s*(?:руб(?:\.|лей|ля)?|тыс|тысяч)",
    re.IGNORECASE,
)

# Аббревиатура кодекса используется в проверке проекта: «статья 310 ГК»
# должно находиться так же надёжно, как «статья 310 ГК РФ».
ABBREVIATIONS = (
    "ГПК", "ГК", "УК", "АПК", "ТК", "СК", "КАС", "КоАП",
)
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
DATE_RE = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\s*года"
)


def _clean_number(raw: str) -> str:
    """«120 000» -> «120000», с защитой от мусора вида «2026-11-04»."""
    digits = re.sub(r"[^\d]", "", raw)
    if not digits or len(digits) > 12:
        return ""
    return digits


@dataclass
class EvidenceItem:
    """Факт с источником."""

    category: str
    text: str
    source_document: str
    source_type: str
    page: int | None = None
    chunk_anchor: str = ""
    # Числа, извлечённые из источника: модель не должна их придумывать.
    amounts: list[str] = field(default_factory=list)
    #: Порядок предложения внутри документа.
    #:
    #: Пакет группируется по категориям, поэтому по индексу в списке
    #: порядок восстановить нельзя: фраза из середины требования
    #: оказывалась рядом с чужой. Без этого номера склейка требования
    #: собирала «взыскать с ответчика расходы по госпошлине» в один
    #: пункт с задолженностью, и сумма терялась. Ноль означает
    #: «порядок неизвестен» — для фактов, созданных вне пакета.
    order: int = 0

    @property
    def citation(self) -> str:
        """Человекочитаемая ссылка для интерфейса (п.36)."""
        parts = [self.source_document]
        if self.page is not None:
            parts.append(f"с. {self.page}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "text": self.text,
            "source": self.source_document,
            "document_type": self.source_type,
            "page": self.page,
            "chunk": self.chunk_anchor,
            "citation": self.citation,
            "amounts": self.amounts,
        }


@dataclass
class EvidencePack:
    """Пакет доказательств для генерации проекта."""

    case_number: str = ""
    court_name: str = ""
    category: str = ""
    items: list[EvidenceItem] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    #: Полные тексты документов дела: имя файла -> текст.
    #:
    #: items хранит только размеченные фразы — по делу real-06 это
    #: 1 241 символ из 4 255. Для исхода требований нужны сплошные
    #: предложения установительной части («исков подлежит удовлетворению
    #: частично», «взыскивает моральный вред в сумме 2000 рублей»), и в
    #: размеченных фразах их может не быть вовсе: классификатор оставляет
    #: по одному признаку на фразу. Читать исход с диска в черновике
    #: нельзя — там только то, что вошло в пакет.
    documents: dict[str, str] = field(default_factory=dict)

    def by_category(self, category: str) -> list[EvidenceItem]:
        return [item for item in self.items if item.category == category]

    def categories_present(self) -> list[str]:
        return sorted({item.category for item in self.items})

    def text_for(self, category: str, limit: int = 8) -> str:
        """Собрать текстовый блок категории для подсказки модели."""
        found = self.by_category(category)[:limit]
        if not found:
            return ""
        return "\n".join(f"- {item.text} (источник: {item.citation})" for item in found)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_number": self.case_number,
            "court_name": self.court_name,
            "category": self.category,
            "participants": self.participants,
            "amounts": self.amounts,
            "dates": self.dates,
            "gaps": self.gaps,
            "items": [item.to_dict() for item in self.items],
        }

    def summary(self) -> str:
        lines = [
            f"Дело № {self.case_number}",
            f"Суд: {self.court_name}",
            f"Категория: {self.category}",
        ]
        if self.participants:
            lines.append("Участники: " + "; ".join(self.participants))
        if self.amounts:
            lines.append("Суммы: " + ", ".join(self.amounts[:8]) + " руб.")
        if self.dates:
            lines.append("Даты: " + "; ".join(self.dates[:8]))
        lines.append("")
        lines.append("Категории с данными: " + ", ".join(self.categories_present()))
        return "\n".join(lines)


def load_template(
    settings: Settings | None = None, *, name: str = "civil_judgment"
) -> dict[str, Any]:
    """Загрузить шаблон судебного акта по имени.

    ``world_judgment`` — вариант для мирового судьи (ч. 3–4 ст. 199
    ГПК РФ): стороны получают резолютивную часть, мотивированное
    решение составляется по заявлению.
    """
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Недопустимое имя шаблона: {name}")
    cfg = settings or get_settings()
    path = cfg.templates_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Шаблон не найден: {path}")
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def _sentences(text: str) -> list[str]:
    """Разбить на осмысленные фразы — они и станут фактами.

    Короткие фразы отбрасываются, но не все: в исковом заявлении
    требование разбито на строки, и строка с суммой бывает очень
    короткой:

        Взыскать с Фирма «...» в пользу Образцов Пётр Петрович
        задолженность по договору поставки № Д-2026/001
        в размере 150000 руб.

    «в размере 150000 руб.» — 22 символа, и при пороге 25 эта строка
    молча выпадала из пакета вместе с суммой. Резолютивная часть
    оказывалась без суммы при полном пакете доказательств.

    Границы фраз не должны резать высказывание посередине. Наивный
    split резал после «ст.» и после номера пункта, цитата в проекте
    решения начиналась с «56 ГПК РФ каждая сторона…», а начало
    «Согласно ст.» уходило в другой фрагмент — владелец дел увидел
    это как «ерунду в тексте». Плюс одиночный перевод строки в файлах
    — это перенос строки, а не конец фразы.
    """
    # Перенос строки внутри абзаца склеиваем; двойной перевод
    # (граница абзаца) остаётся границей фразы.
    text = re.sub(r"(?<![.!?…:])\n(?!\n)", " ", text)

    parts = re.split(r"(?<=[.;!?…])\s+|\n+", text)
    merged: list[str] = []
    for part in parts:
        candidate = part.strip()
        if not candidate:
            continue
        if merged and _continues_after(merged[-1]):
            merged[-1] = f"{merged[-1]} {candidate}"
        else:
            merged.append(candidate)

    kept: list[str] = []
    for candidate in merged:
        if len(candidate) > 25:
            kept.append(candidate)
        elif _carries_critical_data(candidate):
            # Короткая, но несёт сумму, дату или требование — её
            # нельзя терять.
            kept.append(candidate)
    return kept


# Сокращения, после которых точка не заканчивает фразу. Разбор без
# этого правила обрезал цитаты посередине (см. _sentences).
_NOT_A_BREAK = re.compile(
    r"(?:^|[\s(«])"
    r"(?:ст|ч|пп|п|абз|подп|гр|ул|пер|им|тт|рис|стр|лит|обр|др|напр|"
    r"т\.к|т\.е|т\.д|т\.п|и\.о|г|гг|пр)"
    r"\.$",
    re.IGNORECASE,
)
# Номер пункта списка: «1.» — не предложение.
_LIST_MARKER = re.compile(r"^\d{1,3}[.)]$")


def _continues_after(previous: str) -> bool:
    """Правда ли, что предыдущий фрагмент кончился не на границе фразы."""
    return bool(_NOT_A_BREAK.search(previous) or _LIST_MARKER.match(previous))


# Признаки того, что короткая фраза не может быть отброшена.
_CRITICAL = re.compile(
    r"\d[\d\s\u00a0]*\s*(?:руб|коп|₽)"  # сумма
    r"|\d{1,2}\s+(?:январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октяб|нояб|декаб)"
    r"|\bвзыск\w+|\bудовлетвори\w+|\bтреб\w+|\bобяза\w+",
    re.I,
)


def _carries_critical_data(text: str) -> bool:
    """Несёт ли короткая фраза сумму, дату или само требование."""
    return bool(_CRITICAL.search(text))


def extract_money(text: str) -> list[str]:
    """Достать денежные суммы. Числа не доверяем модели.

    Копейки отбрасываются: в «120 000,00 руб.» сумма — это 120000,
    а «00» само по себе суммой не является и, попав в пакет, дало бы
    ложное срабатывание при проверке проекта.
    """
    found: list[str] = []
    for match in MONEY_RE.finditer(text):
        raw = match.group(1)
        digits = _clean_number(raw)
        if not digits:
            continue
        # «120 тыс» = 120000
        if match.group(0).lower().startswith(raw) and "тыс" in match.group(0).lower():
            digits = str(int(digits) * 1000)
        # Сумма короче трёх цифр рядом со словом «руб» — это копейки,
        # а не сумма требования.
        if len(digits) < 3:
            continue
        if digits not in found:
            found.append(digits)
    return found


def extract_dates(text: str) -> list[str]:
    found: list[str] = []
    for day, month, year in DATE_RE.findall(text):
        value = f"{int(day)} {month} {year}"
        if value not in found:
            found.append(value)
    return found


#: Сколько фрагментов берётся из одного документа в одну категорию.
#:
#: Раньше лимит стоял на категорию целиком, и документы в конце списка
#: не давали ничего: договор, платёжка и заключение эксперта оказывались
#: вне пакета, хотя их тип распознавался верно.
PER_DOCUMENT_LIMIT = 6


def _classify(sentence: str, doc_type: str) -> str:
    """Определить категорию факта по содержанию и типу документа."""
    s = sentence.lower()

    # Требования истца распознаются раньше всех остальных правил.
    # Раньше здесь стояли три точные формулировки («истец просит»,
    # «прошу», «требования»), и этого было мало: на живом деле из
    # 46 фактов в категорию «claims» попадал один, причём не
    # требование, а фраза представителя. Резолютивная часть
    # собирается именно из этой категории, и пустая категория означала
    # пустую резолюцию при полном пакете доказательств.
    if looks_like_claim(sentence):
        return "claims"
    if "возражен" in s or "просит отказать" in s:
        return "objections"
    if any(w in s for w in ("обязал", "обязуется", "договор", "постав")):
        return "evidence"
    if any(w in s for w in ("оплат", "перечислен", "руб", "платеж")):
        return "evidence"
    if any(w in s for w in ("суд установил", "установлено", "выводы")):
        return "established_facts"
    if "эксперт" in s or "заключение" in s:
        return "evidence"
    if any(w in s for w in ("статья", "статьи", "ст.", "гк рф", "гпк рф")):
        return "legal_position"
    if doc_type == "PROTOCOL":
        return "protocol_findings"
    return "evidence"


def build_evidence_pack(
    case_number: str,
    court_name: str,
    category: str,
    documents: list[CaseDocument],
    *,
    settings: Settings | None = None,
) -> EvidencePack:
    """Собрать пакет доказательств из документов дела.

    Правило: факт попадает в пакет только вместе с источником.
    Факта без источника не бывает — иначе модель получит «факты из
    воздуха» и подтвердит их как обстоятельства дела.
    """
    pack = EvidencePack(case_number=case_number, court_name=court_name, category=category)

    grouped: dict[str, list[tuple[CaseDocument, str]]] = defaultdict(list)
    all_amounts: list[str] = []
    all_dates: list[str] = []
    # Номер предложения в документе — по id(), потому что одинаковых
    # строк в документе может быть несколько. Нумерация с единицы:
    # ноль означает «порядок неизвестен» для фактов, созданных вне
    # пакета, и не должен совпадать с номером первого предложения.
    sentence_order: dict[int, int] = {}
    sentence_counter = [1]

    for document in documents:
        text = document.text or ""
        if not text.strip():
            continue

        # Сплошной текст документа — для проверок, которым нужны
        # предложения целиком (исход требований: resolve_outcomes).
        pack.documents[document.filename] = text

        for amount in extract_money(text):
            if amount not in all_amounts:
                all_amounts.append(amount)
        for date_value in extract_dates(text):
            if date_value not in all_dates:
                all_dates.append(date_value)

        # Имя истца/ответчика берём из реквизитов первых абзацев документов.
        head = text[:600]
        # Один и тот же человек встречается под разными ролями в разных
        # документах («Ответчик:» в иске и «Поставщик:» в договоре), поэтому
        # собираем имя -> роль, иначе участник дублируется в пакете.
        found_roles: dict[str, str] = {}
        for pattern, role in (
            (r"Истец:\s*([^\n,]+)", "истец"),
            (r"Ответчик:\s*([^\n,]+)", "ответчик"),
            (r"Покупатель:\s*([^\n]+)", "истец"),
            (r"Поставщик:\s*([^\n]+)", "ответчик"),
        ):
            for name in re.findall(pattern, head):
                clean = name.strip().rstrip(".")
                if clean and clean not in found_roles:
                    found_roles[clean] = role

        for clean, role in found_roles.items():
            if not any(p.startswith(clean + " (") for p in pack.participants):
                pack.participants.append(f"{clean} ({role})")

        for sentence in _sentences(text):
            # Порядок предложения в документе. Пакет потом группируется
            # по категориям, и без этой метки восстановить порядок
            # невозможно — а он нужен, чтобы склеить требование из
            # нескольких строк.
            sentence_order[id(sentence)] = sentence_counter[0]
            sentence_counter[0] += 1
            category_name = _classify(sentence, document.document_type)
            grouped[category_name].append((document, sentence))

    # Порядок разделов пакета фиксирован: от известного к спорному.
    order = [
        "claims", "objections", "evidence", "protocol_findings",
        "established_facts", "legal_position", "disputed_facts",
    ]

    # Лимит раньше стоял на категорию, и документы, оказавшиеся в конце
    # списка, не давали ни одного факта: договор, платёжка и заключение
    # эксперта в споре о долге оказывались не учтены, хотя тип их
    # распознавался верно. В деле из демонстрации из шести документов
    # факты шли только из иска, протокола и отзыва.
    #
    # Лимит перенесён на документ: сколько фрагментов берётся из
    # одного файла. Договор в таком деле — доказательство, и молчать о
    # нём хуже, чем процитировать лишнее.
    for category_name in order:
        taken: Counter[str] = Counter()
        for document, sentence in grouped.get(category_name, []):
            name = document.filename
            if taken[name] >= PER_DOCUMENT_LIMIT:
                continue
            taken[name] += 1
            pack.items.append(
                EvidenceItem(
                    category=category_name,
                    text=sentence[:500],
                    source_document=document.filename,
                    source_type=document.document_type,
                    page=document.page_count if document.page_count > 0 else None,
                    chunk_anchor=f"{document.id}",
                    amounts=extract_money(sentence),
                    order=sentence_order[id(sentence)],
                )
            )

    pack.amounts = all_amounts[:20]
    pack.dates = all_dates[:20]

    # Пробелы: честно фиксируем, чего в материалах нет.
    for name, label in (
        ("legal_position", "правовая позиция / подтверждённые нормы права"),
        ("established_facts", "установленные обстоятельства"),
    ):
        if not pack.by_category(name):
            pack.gaps.append(f"нет данных: {label}")

    if not pack.amounts:
        pack.gaps.append("нет данных: суммы требования")

    return pack
