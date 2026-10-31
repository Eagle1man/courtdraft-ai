"""Draft Agent: формирование проекта судебного акта (Фаза 9, ТЗ п.27).

Ключевое решение: разделы генерируются ПО ОТДЕЛЬНО, а не одним длинным
запросом.

Почему: модель на 1.5B параметров, работающая на CPU, на длинном
запросе начинает «плыть» — теряет структуру, повторяется, дорисовывает
факты. Короткий запрос с чёткой инструкцией на один раздел даёт
заметно лучший результат и предсказуемое время.

Второе правило, жёстче: модель НЕ выдумывает числа. Все суммы,
даты и номера берутся из пакета доказательств и подставляются в текст
механически. Если числа нет в пакете — раздел получает пометку
о необходимости проверки.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.agents.evidence_pack import EvidencePack
from app.config import Settings, get_settings
from app.drafting.numeric_guard import mark_unsourced_amounts
from app.drafting.section_quality import unusable_reason
from app.llm import build_llm_provider
from app.llm.base import LLMMessage, LLMProvider, LLMRequest, LLMRole
from app.model_routing.cache import get_response_cache
from app.model_routing.registry import TaskKind, resolve_route
from app.verification.deterministic import check_amounts_by_purpose
from app.verification.subject_of_proof import proof_gaps

logger = logging.getLogger(__name__)

# Пометка, которую ТЗ требует вместо выдумки (п.23, п.27).
NEEDS_REVIEW = "ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ"
NO_LAW_FOUND = "Норма права не найдена. Требуется проверка специалистом."

# Разделы, которые собираются по правилам, а не генерируются моделью.
# Ключи совпадают с ключами шаблона data/templates/civil_judgment.json.
RULES_BUILT_SECTIONS = frozenset(
    {"operative", "objections", "introductory", "descriptive"}
)

#: Якорное начало для разделов, которые модель проваливает чаще всего.
#:
#: Живой прогон (22.10.2026): «УСТАНОВИЛ» приходил на английском
#: (7 % кириллицы, обрыв ровно на лимите токенов), «Мотивировочная» —
#: русским описанием работы («Хорошо, давайте разберемся…»). Оба
#: пропускались проверкой пригодности и исчезали из проекта.
#:
#: Якорь решает обе причины разом: модель начинает с готовой русской
#: фразы, поэтому не тратит токены на рассуждение на другом языке и не
#: описывает свою работу — ей остаётся продолжить заданным голосом.
SECTION_ANCHORS: dict[str, str] = {
    "descriptive": "Истец обратился в суд с иском",
    "established": "Суд установил:",
    "motivating": "Разрешая дело, суд исходит из следующего.",
}

SYSTEM_PROMPT = """Ты — помощник судьи. Пишешь ТОЛЬКО текст одного раздела решения суда.

ПРАВИЛА:
1. Начинай сразу с текста раздела. Никаких заголовков, подзаголовков, нумерации.
2. НИКОГДА не повторяй подписи служебных блоков
   («СВЕДЕНИЯ О ДЕЛЕ», «МАТЕРИАЛЫ ДЕЛА», «СУММЫ», «ДАТЫ», «ИНСТРУКЦИЯ»).
   Это метки для тебя, а не текст для суда.
3. Не выдумывай факты, суммы, даты и номера. Бери только из материала.
4. Если сведений не хватает — напиши "{review}".
5. Если подходящей нормы права в материале нет — напиши "{no_law}".
6. Пиши по-русски, в официально-деловом стиле, короткими предложениями."""


@dataclass
class SectionDraft:
    """Сгенерированный раздел проекта."""

    key: str
    name: str
    text: str
    # Отметка, получен ли текст от настоящей модели или заглушки.
    is_mock: bool = True
    citations: list[str] = field(default_factory=list)
    needs_review: bool = False
    latency_ms: int = 0
    error: str = ""
    #: Кто собрал текст: "model" — языковая модель, "rules" — правила.
    #:
    #: Проверка «число отсутствует в материалах» написана для текста
    #: модели и только он может содержать выдумку. В разделах, собранных
    #: правилами, числа — собственная арифметика проекта (итог,
    #: копейки, госпошлина): она не бывает в материалах в готовом виде
    #: и проверяется отдельно (scripts/e2e_audit.py). Раньше различия
    #: не было, и верификация помечала собственный итог резолюции
    #: выдумкой модели — 8 CRITICAL на проекте без единой выдумки.
    built_by: str = "model"
    #: Чего нет в материалах дела. Показывается отдельным блоком, а не
    #: строкой внутри текста: в проекте акта не должно стоять
    #: «ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ» — это техническая пометка, а не
    #: текст судебного решения (см. _strip_markers).
    gaps: list[str] = field(default_factory=list)


@dataclass
class DraftResult:
    """Проект судебного акта целиком."""

    case_number: str
    sections: list[SectionDraft] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    total_latency_ms: int = 0

    @property
    def text(self) -> str:
        """Собрать полный текст проекта по шаблону."""
        parts: list[str] = []
        for section in self.sections:
            parts.append(f"{section.name.upper()}\n\n{section.text}")
        return "\n\n".join(parts)

    @property
    def sections_needing_review(self) -> list[SectionDraft]:
        return [s for s in self.sections if s.needs_review]

    def sources(self) -> list[str]:
        """Источники проекта, по одному на документ.

        Раньше дедупликация шла по полной строке цитаты, и один и тот же
        договор считался двумя источниками, когда на него ссылались с
        разными страницами («03_contract.txt» и «03_contract.txt, стр. 3»).
        Счётчик «источников» расходился с числом документов, и судья видел
        в проекте больше доказательств, чем их в деле. Ключом берётся имя
        документа до запятой — страница остаётся в первой встречной цитате.
        """
        out: list[str] = []
        seen: set[str] = set()
        for section in self.sections:
            for citation in section.citations:
                base = citation.split(",")[0].strip()
                # Страница в скобках — вариант того же документа
                # (дефект D1): «03.txt» и «03.txt (стр. 3)» — один источник.
                base = re.sub(r"\s*\(стр\.\s*\d+\)\s*$", "", base).strip()
                if base and base not in seen:
                    seen.add(base)
                    out.append(citation)
        return out

    def stats(self) -> dict[str, int]:
        return {
            "sections": len(self.sections),
            "sections_needing_review": len(self.sections_needing_review),
            "characters": len(self.text),
            "citations": len(self.sources()),
        }


def _format_amounts(pack: EvidencePack) -> str:
    if not pack.amounts:
        return f"суммы в материалах не указаны — укажи {NEEDS_REVIEW}"
    return "суммы, встречающиеся в материалах (руб.): " + ", ".join(pack.amounts[:10])


def _format_participants(pack: EvidencePack) -> str:
    if not pack.participants:
        return "участники в материалах не указаны"
    return "; ".join(pack.participants)


#: Документы, которые модель обязана видеть целиком.
#:
#: Разметка пакета оставляет по одной фразе на признак, и в подсказку
#: попадали обрывки: «района Хамовники … ул. Пречистенка, д». По ним
#: нельзя написать ни описательную часть, ни хронологию. Сплошной текст
#: иска, возражения и расчёта даёт модели связный контекст: кто истец,
#: кто ответчик, когда договор, сколько передано и сколько возвращено.
_KEY_DOC_RE = re.compile(
    r"иск|заявлен|возражен|отзыв|расч[её]т|претенз|договор|расписк",
    re.IGNORECASE,
)

#: Бюджет сплошного текста на один запрос, в знаках.
#:
#: OLLAMA_NUM_CTX по умолчанию 4096 токенов, и часть уходит на
#: инструкцию и ответ. Живой прогон (31.10.2026) показал: при 12 000 и
#: 8 000 знаках документов vikhr-7b теряла задание и отвечала пустым
#: текстом. Отдаём только те документы, которые нужны разделу.
FULL_TEXT_BUDGET = 4_000
FULL_TEXT_PER_DOC = 2_500

#: Какие документы нужны конкретному разделу.
#:
#: Правило: разделу отдаётся то, из чего он пишется. Описательная часть
#: и хронология строятся по иску и расчёту; мотивировочная — по
#: возражениям и иску. Когда модель получала все шесть документов
#: сразу, она теряла задание.
_SECTION_DOCS: dict[str, tuple[str, ...]] = {
    "descriptive": ("иск", "заявлен", "расч", "договор", "расписк", "претенз"),
    "established": ("иск", "заявлен", "расч", "договор", "расписк", "претенз"),
    "motivating": ("возраж", "отзыв", "иск", "заявлен", "расч"),
}


def _full_document_blocks(
    pack: EvidencePack,
    section_key: str = "",
    budget: int = FULL_TEXT_BUDGET,
    per_doc: int = FULL_TEXT_PER_DOC,
) -> tuple[list[str], list[str]]:
    """Сплошные тексты документов, нужных разделу, для подсказки модели."""
    needles = _SECTION_DOCS.get(section_key, ())
    blocks: list[str] = []
    citations: list[str] = []
    used = 0
    for name, text in pack.documents.items():
        if used >= budget:
            break
        if not text:
            continue
        low = name.lower()
        if needles:
            if not any(needle in low for needle in needles):
                continue
        elif not _KEY_DOC_RE.search(name):
            continue
        chunk = text.strip()
        room = min(per_doc, budget - used)
        if room <= 200:
            break
        if len(chunk) > room:
            chunk = chunk[:room].rstrip() + " …"
        blocks.append(f"[{_document_title(name)}]\n{chunk}")
        citations.append(name)
        used += len(chunk)
    return blocks, citations


def build_section_prompt(
    section: dict[str, Any],
    pack: EvidencePack,
) -> tuple[str, list[str]]:
    """Собрать запрос на один раздел.

    Возвращает (текст запроса, ссылки-источники, использованные в разделе).
    """
    categories: list[str] = section.get("evidence_categories", [])
    name = section["name"]
    purpose = section.get("purpose", "")
    hint = section.get("prompt_hint", "")

    evidence_blocks: list[str] = []
    citations: list[str] = []

    for category in categories:
        block = pack.text_for(category, limit=6)
        if block:
            evidence_blocks.append(f"[{category}]\n{block}")
            for item in pack.by_category(category)[:6]:
                if item.citation not in citations:
                    citations.append(item.citation)

    # Сплошные тексты ключевых документов. Размеченные фразы выше
    # отвечают на вопрос «что где сказано», но не дают связного
    # рассказа о деле. Без него модель не может ни изложить требования
    # истца, ни выстроить хронологию, и раздел уходит в пометку
    # «материалы не позволяют подготовить раздел».
    full_blocks, full_citations = _full_document_blocks(pack, section.get("key", ""))
    if full_blocks:
        evidence_blocks.append(
            "[полные тексты ключевых документов]\n" + "\n\n".join(full_blocks)
        )
        for name in full_citations:
            if name not in citations:
                citations.append(name)

    if pack.gaps:
        evidence_blocks.append(
            "[пробелы в материалах]\n" + "\n".join(f"- {gap}" for gap in pack.gaps)
        )

    if not evidence_blocks:
        evidence_blocks.append(
            "[материалы по этому разделу отсутствуют — раздел требует проверки специалистом]"
        )

    # Промпт намеренно компактный и без громоздких подписанных блоков.
    # Модель на 1.5B копирует в ответ всё, что выглядит как содержимое,
    # и служебные подписи предыдущей версии попадали прямо в проект.
    facts = (
        f"Дело № {pack.case_number}. Суд: {pack.court_name}. "
        f"Категория: {pack.category}. "
        f"Участники: {_format_participants(pack)}. "
        f"{_format_amounts(pack)}. "
        f"Даты: {'; '.join(pack.dates[:8]) or 'не указаны'}."
    )

    request = f"""Напиши раздел «{name}» решения суда.

Назначение раздела: {purpose}
{hint}

Сведения о деле: {facts}

Материалы:
{chr(10).join(evidence_blocks)}

Сейчас пиши текст раздела «{name}». Не повторяй названия блоков.
Сначала найди нужные сведения в полных текстах документов. Пометку
«{NEEDS_REVIEW}» ставь только если данных там действительно нет.
Перечисляй факты, а не названия документов.
Если нормы права в материале нет — напиши: {NO_LAW_FOUND}"""

    return request, citations


def _postprocess(text: str, section: dict[str, Any]) -> tuple[str, bool]:
    """Почистить ответ модели.

    Модель часто повторяет заголовок раздела или добавляет нумерацию —
    это ломает структуру проекта.
    """
    name = section["name"].upper()
    needs_review = False

    cleaned = text.strip()

    # Убираем повторный заголовок в начале.
    lines = cleaned.split("\n")
    while lines and (not lines[0].strip() or name in lines[0].upper()[:20]):
        if lines and name not in lines[0].upper()[:20]:
            break
        lines.pop(0)
    cleaned = "\n".join(lines).strip()

    # Убираем служебные префиксы. Модель часто начинает раздел с
    # «Резолютивная часть:» или «Раздел 3:» — это ломает структуру
    # итогового документа, где раздел уже назван заголовком.
    cleaned = re.sub(
        r"^\s*(раздел\s*\d*\s*[:—-]?\s*|текст\s*[:—-]|ответ\s*[:—-]|"
        r"[а-яё]{3,20}\s+часть\s*[:—-]?)\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    if NEEDS_REVIEW in cleaned:
        needs_review = True
    lowered = cleaned.lower()
    if "не хватает" in lowered or "недостаточно данных" in lowered:
        needs_review = True
    if NO_LAW_FOUND.split(".")[0] in cleaned:
        needs_review = True

    # Раздел от заглушки нельзя пропускать как годный: настоящая модель
    # не вызывалась, и проверить было нечего. Без этой пометки проект в
    # режиме mock выглядел бы «чистым» — то есть притворялся бы готовым.
    if "MOCK-ОТВЕТ" in cleaned:
        needs_review = True

    # Горизонтальные разделители — оформление ответа модели, в тексте
    # проекта они не нужны: разделы разделяются заголовками.
    cleaned = re.sub(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$", "", cleaned, flags=re.MULTILINE)

    # Убираем markdown-заголовки: модель оформляет раздел как
    # «### Описательная часть», а в проекте акта раздел уже назван
    # заголовком выше по тексту.
    lines = [
        ln for ln in cleaned.split("\n")
        if not re.match(r"^\s*#{1,6}\s", ln)
    ]
    cleaned = "\n".join(lines).strip()

    # Жирное выделение — это разметка ответа модели, а не текст документа.
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned, flags=re.DOTALL)

    if not cleaned:
        needs_review = True
        cleaned = f"Материалы дела не позволяют подготовить раздел. {NEEDS_REVIEW}"

    cleaned = _strip_leading_heading(cleaned, section["name"])
    cleaned = _drop_heading_repeats(cleaned, section["name"])
    cleaned = _drop_echo(cleaned)
    cleaned = _drop_repeats(cleaned)

    if not cleaned:
        needs_review = True
        cleaned = f"Материалы дела не позволяют подготовить раздел. {NEEDS_REVIEW}"
        return cleaned, needs_review

    # Обрыв на середине фразы — признак того, что модель упёрлась в лимит
    # ответа. Такой раздел нельзя нести дальше как готовый: обрыв должен
    # быть виден специалисту, а не выглядеть как конец мысли.
    if cleaned.rstrip()[-1:] not in ".!?;:»)]}\"'":
        needs_review = True
        cleaned = (
            cleaned.rstrip()
            + f"\n\n[текст оборван по лимиту ответа] {NEEDS_REVIEW}"
        )

    return cleaned, needs_review


def _strip_markers(section: SectionDraft) -> None:
    """Убрать пометки о нехватке данных из текста раздела.

    Правило продукта: в тексте проекта не стоит «ТРЕБУЕТ ПРОВЕРКИ
    СПЕЦИАЛИСТОМ». Это техническая инструкция системе, а не текст
    судебного решения: она попадала в печатную форму, и судья видел в
    акте строчку про «специалиста». Всё, чего нет в материалах, уходит
    в блок «Не хватает данных» (SectionDraft.gaps) — тот же
    перечень, но вне текста акта.

    Правила разбора строки:

    * «Состав суда: ТРЕБУЕТ ПРОВЕРКИ…» — строка не идёт в текст, ярлык
      становится пробелом.
    * «[текст оборван по лимиту ответа] ТРЕБУЕТ…» — то же самое,
      но ярлык сохраняется как есть.
    * Строка без ярлыка целиком считается пометкой: и в тексте акта
      не остаётся, и в пробелы она попадает со своей формулировкой.
    """
    if NEEDS_REVIEW not in section.text and NO_LAW_FOUND not in section.text:
        return

    kept: list[str] = []
    for line in section.text.split("\n"):
        if NEEDS_REVIEW not in line and NO_LAW_FOUND not in line:
            kept.append(line)
            continue
        rest = line
        for marker in (NEEDS_REVIEW, NO_LAW_FOUND):
            rest = rest.replace(marker, "")
        rest = rest.strip().strip(",:;- ").strip()
        original = line.strip()
        if ":" in original:
            label = original.split(":", 1)[0].strip()
            note = f"{label}: данных нет в материалах дела"
        else:
            note = rest
        if note:
            section.gaps.append(note)
        if len(rest) >= 60:
            # За пометкой стоит настоящее предложение — его оставляем.
            kept.append(rest)

    section.text = "\n".join(kept).strip()
    section.needs_review = True


# Служебные подписи, которые модель переносит в текст решения.
# Это не содержание проекта, а метаданные запроса.
_ECHO_MARKERS = (
    "суммы, встречающиеся в материалах",
    "сведения о деле:",
    "материалы:",
    "материалы дела",
    "назначение раздела:",
    "сейчас пиши текст",
    "если сведений не хватает",
    "если нормы права в материале",
)


def _drop_echo(text: str) -> str:
    """Убрать строки, где модель пересказывает сам запрос.

    Модель на 1.5B склонна цитировать входные данные дословно. В проекте
    судебного акта это выглядит как служебная простыня, а не как текст.
    """
    kept: list[str] = []
    for raw in text.split("\n"):
        lowered = raw.lower()
        if any(marker in lowered for marker in _ECHO_MARKERS):
            continue
        kept.append(raw)
    return "\n".join(kept).strip()


def _strip_leading_heading(text: str, name: str) -> str:
    """Убрать название раздела в начале текста, если модель его повторила.

    Сделано ПОСЛЕ снятия markdown-разметки: «**РЕШИЛ**» превращается в
    «РЕШИЛ» только на этом шаге, и раньше проверка не видела заголовок.
    Удаляются подряд идущие повторы вместе с пустыми строками между ними.
    """
    target = re.sub(r"\W+", "", name.upper())
    if not target:
        return text

    lines = text.split("\n")
    index = 0
    # Пропускаем ведущие пустые строки.
    while index < len(lines) and not lines[index].strip():
        index += 1

    removed = 0
    while index < len(lines):
        key = re.sub(r"\W+", "", lines[index]).upper()
        if key == target:
            index += 1
            removed += 1
            # Поглощаем пустые строки после заголовка, но только если
            # дальше снова идёт тот же заголовок.
            probe = index
            while probe < len(lines) and not lines[probe].strip():
                probe += 1
            if probe < len(lines) and re.sub(r"\W+", "", lines[probe]).upper() == target:
                index = probe
            continue
        break

    if not removed:
        return text
    return "\n".join(lines[index:]).strip()


def _build_operative_section(section: dict[str, Any], pack: EvidencePack) -> SectionDraft:
    """Собрать резолютивную часть по правилам, без участия модели.

    Требования извлекаются из пакета доказательств, где лежат фразы
    из материалов дела вместе с указанием документа. Затем
    app.drafting.operative переводит их в установленную форму.

    Неразобранные требования не выбрасываются молча: они попадают в
    раздел отдельным абзацем, чтобы судья увидел, что часть
    требований осталась вне проекта решения.
    """
    from app.drafting.operative import build_operative
    from app.drafting.requirements import (
        extract_budget,
        extract_counter_claims,
        extract_fee_and_costs,
        extract_parties,
        extract_procedure,
        extract_requirements,
    )

    money, others, unrecognised = extract_requirements(pack)
    counters = extract_counter_claims(pack)
    fee, costs = extract_fee_and_costs(pack)
    parties = extract_parties(pack)
    procedure = extract_procedure(pack)
    budget = extract_budget(pack)
    result = build_operative(
        money, others, counter_claims=counters, fee_paid=fee,
        costs=costs, parties=parties, procedure=procedure, budget=budget,
    )

    text = result.render()

    citations: list[str] = [claim.source for claim in money if claim.source]
    citations.extend(claim.source for claim in others if claim.source)
    citations.extend(claim.source for claim in counters if claim.source)

    return SectionDraft(
        key=section["key"],
        name=section["name"],
        text=text,
        # Раздел собран правилами, а не получен от заглушки модели.
        # Флаг «получено от настоящей модели» здесь был бы неверным.
        is_mock=False,
        citations=citations,
        needs_review=bool(result.gaps) or bool(unrecognised),
        built_by="rules",
        # Неразобранные требования — тоже пробел: судья должен увидеть,
        # что часть требований осталась вне разбора, но в тексте акта
        # эта пометка стоять не должна.
        gaps=[f"не разобрано: {item}" for item in unrecognised]
        + list(result.gaps),
    )


def _side_name(pack: EvidencePack, role: str) -> str:
    """Имя стороны из пакета: «ФИО (роль)» → ФИО при совпадении роли."""
    marker = f" ({role})"
    for entry in pack.participants:
        if entry.endswith(marker):
            return entry[: -len(marker)]
    return ""


def _document_title(filename: str) -> str:
    """Название документа без расширения файла.

    В тексте раздела «Возражение на исковое заявление: «…»» читается
    нормально, а «Возражение на исковое заявление.docx: «…»» — как
    техническая деталь. Полное имя файла остаётся в citations: по нему
    проверка сверяет ссылку с настоящим документом дела.
    """
    return re.sub(r"\.(?:docx?|pdf|txt|md|rtf)$", "", filename or "", flags=re.I)


def _dispute_subject(pack: EvidencePack) -> str:
    """Предмет спора — формулировка из материалов, а не первая фраза.

    Раньше здесь бралась первая строка категории «claims», и на живом
    деле это была фраза из возражения ответчика: вводная часть
    объявляла предметом спора чужие слова (дело 1w121, 4 из 7 цитат в
    разделе «Доводы ответчика» были не от той стороны).

    Порядок поиска — от самого точного источника к самому общему:

    1. Заголовок иска («ИСКОВОЕ ЗАЯВЛЕНИЕ / о взыскании суммы займа и
       процентов…») — так предмет называет сам истец.
    2. Формулировка из текста дела («по иску A к B о взыскании…») —
       так предмет называют участники и суд.
    3. Первая фраза требования из иска — если заголовка нет.
    """
    claim_texts = [
        text
        for name, text in pack.documents.items()
        # «иск.docx» не содержит «исков», поэтому фильтр был шире формы
        # слова: иначе настоящий иск не попадал в поиск и предмет спора
        # брался из чужого документа. Латиница добавлена по той же
        # причине: файлы часто называют claim.pdf / statement.docx, и
        # из-за этого предмет спора терялся на реальных выгрузках.
        if re.search(
            r"иск|заявлени|claim|statement|lawsuit|isk|zayav",
            name.lower(),
        )
    ]
    for text in claim_texts:
        found = re.search(
            # «ИСКОВОЕ ЗАЯВЛЕНИЕ» + перенос строки + «о взыскании …».
            # \s* уже перебирает переводы строки, поэтому лишний \n+
            # после него ломал совпадение и предмет спора падал на
            # первую фразу требования.
            r"исковое\s+заявление\s*(?:[^\n]*\n\s*)?(о\s+[^\n]{4,200})",
            text,
            re.IGNORECASE,
        )
        if found:
            return found.group(1).strip()
    for text in pack.documents.values():
        found = re.search(
            # «по иску Смирновой О.В. к Кузнецову А.П. о взыскании …».
            # Точки в инициалах внутри промежутка разрешены: иначе
            # совпадения нет и предмет спора брался из первой фразы.
            r"по\s+иску\s+[^\n;,]{2,140}?\sо\s+([^\n;,]{4,140})",
            text,
            re.IGNORECASE,
        )
        if found:
            return f"о {found.group(1).strip()}"
    claims = pack.by_category("claims")
    if claims:
        return claims[0].text.strip()
    return ""


def _build_intro_section(section: dict[str, Any], pack: EvidencePack) -> SectionDraft:
    """Вводная часть по ч. 2 ст. 198 ГПК РФ — правилами, не моделью.

    Рецензент (п. 11.1): в демо вводная пустая, хотя суд, номер дела и
    стороны есть в карточке. Модель на 1,5B реквизиты теряла, а
    выдумывать состав суда и дату решения нельзя — их в материалах нет.
    Всё известное из пакета ставится строкой; чего в материалах нет —
    честной отметкой, которую судья заполнит сам.
    """
    lines: list[str] = ["Именем Российской Федерации", ""]
    # Реквизиты берутся из документов дела, а не из карточки: карточку
    # заводят руками и она расходится с материалами (на живом деле —
    # «суд советского района» и «1w121» против настоящих реквизитов).
    # Расхождение с карточкой показывает проверка.
    court = pack.court_from_docs or pack.court_name
    number = pack.case_number_from_docs or pack.case_number
    if court:
        lines.append(court)
    if number:
        lines.append(f"Дело № {number}")
    lines.append("")

    for role in ("истец", "ответчик"):
        name = _side_name(pack, role)
        lines.append(f"{role.capitalize()}: {name or NEEDS_REVIEW}")

    subject = _dispute_subject(pack)
    if subject:
        if len(subject) > 400:
            subject = subject[:400].rstrip() + "…"
        lines.append(f"Предмет спора: {subject}")
    else:
        lines.append(f"Предмет спора: {NEEDS_REVIEW}")

    # Состав суда, секретарь и дата решения — реквизиты судебного акта,
    # которых нет ни в иске, ни в договоре. Модели они недоступны, а
    # без отметки выглядели бы как уже заполненные.
    lines.append(f"Состав суда: {NEEDS_REVIEW}")
    lines.append(f"Секретарь судебного заседания: {NEEDS_REVIEW}")
    lines.append(f"Дата принятия решения: {NEEDS_REVIEW}")

    return SectionDraft(
        key=section["key"],
        name=section["name"],
        text="\n".join(lines),
        # Реквизиты взяты из материалов, а не из заглушки модели.
        is_mock=False,
        needs_review=True,
        built_by="rules",
    )


def _build_descriptive_section(
    section: dict[str, Any], pack: EvidencePack
) -> SectionDraft:
    """Описательная часть по правилам, без участия модели.

    Почему правилами: на этом промпте `vikhr-7b` возвращает немедленный
    конец ответа — проверено на уровне API Ollama (`eval_count=1`,
    `done_reason=stop`, пустой `content`), причём промпт влезает в
    контекст. Модель 1,5B тот же раздел пишет, но короче и с потерями.

    Содержание — только факты из пакета: кто, к кому, с чем обратился и
    что указано в обоснование. Требования цитируются дословно с
    указанием документа: пересказ требований истца моделью уже приводил
    к подмене суммы в проекте. Оценок и выводов в разделе нет — это
    работа суда.
    """
    plaintiff = _side_name(pack, "истец")
    defendant = _side_name(pack, "ответчик")
    subject = _dispute_subject(pack)

    if plaintiff and defendant:
        opening = f"Истец {plaintiff} обратился в суд с иском к {defendant}"
    elif plaintiff:
        opening = f"Истец {plaintiff} обратился в суд с иском"
    elif defendant:
        opening = f"В суд предъявлен иск к {defendant}"
    else:
        opening = "В суд предъявлен иск"

    if subject and re.match(r"^о[\s.]", subject, re.IGNORECASE):
        # Предмет спора годится в предложение «с иском …», только если это
        # формулировка вида «о взыскании …». Иначе это первая фраза
        # требования, и «с иском к X Взыскать задолженность» — не текст акта.
        opening += f" {subject}"
    lines: list[str] = [opening + "."]

    claims = pack.by_category("claims")[:5]
    citations: list[str] = []
    if claims:
        lines.append("")
        lines.append("В обоснование иска указано:")
        for index, item in enumerate(claims, start=1):
            lines.append(
                f"{index}. {_document_title(item.source_document)}: «{item.text}»"
            )
            if item.source_document and item.source_document not in citations:
                citations.append(item.source_document)
    else:
        lines.append("")
        lines.append(
            f"Требования истца в материалах дела не выделены: {NEEDS_REVIEW}"
        )

    if pack.by_category("objections"):
        lines.append("")
        lines.append(
            "Ответчик представил возражения на иск; их содержание приведено "
            "в разделе «Доводы ответчика»."
        )

    if claims:
        lines.append("")
        lines.append(
            "Изложено по материалам дела. Оценка доказательств в этом "
            "разделе не приводится."
        )

    return SectionDraft(
        key=section["key"],
        name=section["name"],
        text="\n".join(lines),
        # Текст собран правилами из материалов, а не заглушкой модели.
        is_mock=False,
        citations=citations,
        needs_review=not claims,
        built_by="rules",
    )


#: Реквизиты и адрес: «119034, г. Москва, ул. Пречистенка, д. 12».
_NOISE_RE = re.compile(
    r"(?:г\.|гор\.|ул\.|пр-т|просп\.|пер\.|д\.\s*\d|дом\s+\d|кв\.|корп\.|"
    r"стр\.|обл\.|р-н|район[ае]?\s|индекс|тел\.|факс|e-?mail|ИНН|ОГРН|КПП|"
    r"БИК|р/с|к/с|банк)",
    re.IGNORECASE,
)

#: Почтовый индекс: шесть цифр подряд. В доводах он не встречается.
_INDEX_RE = re.compile(r"\b\d{6}\b")

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?=\s|$)")

#: Сокращения, после которых точка не заканчивает предложение.
#: Без этого «…района Хамовники г. Москвы находится…» резалось после
#: «г.», и в доводы попадал обрывок «Москвы находится гражданское дело».
_ABBREVIATIONS = ("г", "ст", "п", "ч", "руб", "коп", "им", "д", "ул", "т", "е")

_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]{2,}")


def _split_sentences(text: str) -> list[str]:
    """Разбить текст на предложения, не разрезая сокращения и инициалы."""
    parts: list[str] = []
    start = 0
    for match in _SENTENCE_SPLIT_RE.finditer(text):
        before = text[max(0, match.start() - 4): match.start()].lower().strip()
        tail = before.split()[-1] if before.split() else ""
        if tail.rstrip(".") in _ABBREVIATIONS:
            continue
        # Инициалы: одна заглавная буква перед точкой («Иванов А. П.»).
        if len(tail) == 1 and tail.isalpha():
            continue
        parts.append(text[start: match.end()])
        start = match.end()
    tail = text[start:]
    if tail.strip():
        parts.append(tail)
    return parts


def _is_requisite_line(fragment: str) -> bool:
    """Строка состоит из шапки, адреса или реквизитов, а не из довода."""
    if _INDEX_RE.search(fragment):
        return True
    if _NOISE_RE.search(fragment):
        return True
    # «г. Москва, ул. Ленина, 5» после отбрасывания коротких слов
    # оставляет мало настоящих слов — такой строке в доводах не место.
    return len(_WORD_RE.findall(fragment)) < 4


def _objection_fragments(text: str) -> list[str]:
    """Выбрать из текста возражения фразы, которые являются доводами.

    Раньше в раздел попадал весь элемент разметки целиком, и вместе с
    доводом там оказывался адрес суда («района Хамовники … ул.
    Пречистенка, д»). Для судьи это мусор: довод — это утверждение о
    факте или просьба, а не шапка документа.

    Фильтр работает по исключению, а не по списку ключевых слов:
    «Договор расторгнут по соглашению сторон» — настоящий довод, хотя
    слова «не согласен» в нём нет. Отбрасывается только то, что
    заведомо не является доводом: адрес, индекс, реквизиты, слишком
    короткая строка.
    """
    fragments: list[str] = []
    for raw in _split_sentences((text or "").replace("\n", " ")):
        fragment = raw.strip().strip("«»\"' \t")
        if len(fragment) < 25:
            continue
        if _is_requisite_line(fragment):
            continue
        fragments.append(fragment)
    return fragments


def _build_established_section(
    section: dict[str, Any], pack: EvidencePack
) -> SectionDraft:
    """«УСТАНОВИЛ» из материалов, когда модель не дала пригодного текста.

    Обе Vikhr-модели на этом промпте отвечают короткой фразой-заголовком
    («суд установил следующее:», «обстоятельства установлены верно») и
    останавливаются — проверено на уровне API Ollama, `eval_count` 5–8.
    Оставлять раздел пустым нельзя: это дыра в акте, а не «нет данных».

    Содержание — только факты из материалов с указанием документа. Оценок,
    выводов и квалификации в разделе нет: установление обстоятельств —
    работа суда, а не подготовительной машины, поэтому раздел всегда
    помечен «требуется проверка».
    """
    facts: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Хронология в материалах — это требования истца и факты из
    # документов; берём их по порядку и без повторов.
    for category in ("claims", "evidence"):
        for item in pack.by_category(category):
            key = re.sub(r"\W+", "", item.text.lower())[:80]
            if not key or key in seen:
                continue
            # Реквизиты и шапку в обстоятельства не тащим: это не факт.
            if _NOISE_RE.search(item.text) and not re.search(r"\d", item.text):
                continue
            seen.add(key)
            facts.append((item.text, item.source_document))
            if len(facts) >= 5:
                break
        if len(facts) >= 5:
            break

    if not facts:
        return SectionDraft(
            key=section["key"],
            name=section["name"],
            text="Материалы дела не содержат обстоятельств, которые можно "
            "изложить как установленные.",
            is_mock=False,
            needs_review=True,
            built_by="rules",
        )

    lines = ["СУД УСТАНОВИЛ:", ""]
    citations: list[str] = []
    for index, (fact, source) in enumerate(facts, start=1):
        lines.append(f"{index}. {_document_title(source)}: «{fact}»")
        if source and source not in citations:
            citations.append(source)
    lines.append("")
    lines.append(
        "Изложено по материалам дела. Оценка доказательств и выводы "
        "по существу спора в разделе не приводятся."
    )
    return SectionDraft(
        key=section["key"],
        name=section["name"],
        text="\n".join(lines),
        is_mock=False,
        citations=citations,
        needs_review=True,
        built_by="rules",
    )


def _build_objections_section(
    section: dict[str, Any], pack: EvidencePack
) -> SectionDraft:
    """Собрать «Доводы ответчика» по правилам, без участия модели.

    Позиция ответчика — то, что рецензент жаловал разбором п. 75:
    модель на 1.5B пересказывала возражения с искажениями, и довод,
    который ответчик не заявлял, попадал в проект решения. Здесь
    возражения цитируются дословно и по одному абзацу с указанием
    документа. Оценки доводов в разделе нет и быть не должно:
    оценка — работа суда, а не подготовительной машины.
    """
    objections = pack.by_category("objections")
    if not objections:
        # Молчаливый пропуск раздела хуже честной отметки: рецензент
        # (п. 45) ловил именно случаи, когда отсутствие блока выглядело
        # как «у ответчика возражений не было», хотя он их заявлял.
        return SectionDraft(
            key=section["key"],
            name=section["name"],
            text=(
                "Возражения ответчика в материалах дела не обнаружены. "
                "Если отзыв ответчика был, требуется его загрузка: "
                f"{NEEDS_REVIEW}"
            ),
            needs_review=True,
            # Текст собран правилами из материалов дела — это не заглушка
            # mock-провайдера, иначе движок проверки и интерфейс показывают
            # честную секцию как мок (см. вторую ветку ниже).
            is_mock=False,
            built_by="rules",
        )

    paragraphs: list[str] = []
    citations: list[str] = []
    seen: set[str] = set()

    for item in objections:
        for fragment in _objection_fragments(item.text):
            key = re.sub(r"\W+", "", fragment.lower())
            if not key or key in seen:
                continue
            seen.add(key)
            paragraphs.append(
                f"{len(paragraphs) + 1}. "
                f"{_document_title(item.source_document)}: «{fragment}»"
            )
            if item.source_document and item.source_document not in citations:
                citations.append(item.source_document)
            if len(paragraphs) >= 7:
                break
        if len(paragraphs) >= 7:
            break

    if not paragraphs:
        # Возражения есть, но все строки оказались реквизитами и шапкой
        # документа. Честнее сказать это прямо, чем показывать адрес
        # суда как «довод ответчика».
        return SectionDraft(
            key=section["key"],
            name=section["name"],
            text=(
                "В материалах дела есть отзыв ответчика, но его доводы "
                "не удалось выделить: документ содержит только реквизиты "
                f"и шапку. Требуется прочитать отзыв: {NEEDS_REVIEW}"
            ),
            is_mock=False,
            needs_review=True,
            built_by="rules",
        )

    text = "\n".join(paragraphs)
    text += (
        "\n\nДоводы приведены из материалов дела. Оценка доводов "
        "судом не производилась."
    )
    return SectionDraft(
        key=section["key"],
        name=section["name"],
        text=text,
        # Цитаты собраны правилами из материалов дела, а не заглушкой.
        is_mock=False,
        citations=citations,
        built_by="rules",
    )


def _drop_heading_repeats(text: str, name: str) -> str:
    """Убрать повторы названия раздела.

    Модель иногда печатает заголовок дважды подряд («РЕШИЛ / РЕШИЛ»).
    В проекте раздел уже назван заголовком выше, поэтому дубль только
    путает структуру документа.
    """
    target = re.sub(r"\W+", "", name.upper())
    if not target:
        return text

    kept: list[str] = []
    seen_heading = False
    for raw in text.split("\n"):
        key = re.sub(r"[\W*#]+", "", raw).strip().upper()
        is_heading = bool(key) and key == target
        if is_heading and seen_heading:
            continue
        # Пустая строка не должна сбрасывать счётчик: модель печатает
        # «РЕШИЛ» / пустая / «РЕШИЛ», и это всё тот же дубль.
        seen_heading = is_heading or (not raw.strip() and seen_heading)
        kept.append(raw)
    return "\n".join(kept).strip()


def _drop_repeats(text: str) -> str:
    """Убрать повторяющиеся строки.

    Модель на 1.5B часто зацикливается и печатает одну и ту же фразу
    несколько раз подряд. В проекте судебного акта это выглядит как
    неисправность, хотя текст был получен корректно.
    """
    seen: set[str] = set()
    kept: list[str] = []

    for raw in text.split("\n"):
        key = re.sub(r"\W+", "", raw.lower())
        # Короткие строки не сравниваем: «1.» или «-» повторяются законно.
        if len(key) > 25:
            if key in seen:
                continue
            seen.add(key)
        kept.append(raw)

    return "\n".join(kept).strip()


async def generate_draft(
    pack: EvidencePack,
    template: dict[str, Any],
    *,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
    only_sections: list[str] | None = None,
) -> DraftResult:
    """Сгенерировать проект судебного акта по разделам.

    Раздел резолютивной части собирается по правилам и модели не
    передаётся. Причина: это единственная часть решения, где ошибка
    непоправима — в ней суд постановляет, кому и сколько платить.
    Модель на 1,5B путает стороны именно тут, и лечится это не длиной
    промпта, а жёсткой грамматикой (см. app.drafting.operative).
    """
    cfg = settings or get_settings()
    # Кэш включается только на штатном пути (провайдер из настроек).
    # Если провайдер передан явно, это инъекция: тесты и стенды ждут
    # настоящих вызовов, а не ответа из чужого прогона.
    cache = get_response_cache(cfg) if provider is None else None
    if provider is None:
        # Маршрут по умолчанию совпадает с OLLAMA_MODEL, поэтому пока
        # модель одна, поведение не меняется. Переопределение приходит
        # только переменной окружения — «на всякий случай» модель не
        # подменяется, иначе качество нельзя было бы сравнить.
        route = resolve_route(TaskKind.SECTION_DRAFT, cfg)
        if route.source != "default" and route.model != cfg.ollama_model:
            cfg = cfg.model_copy(update={"ollama_model": route.model})
        llm = build_llm_provider(cfg)
    else:
        llm = provider

    sections_to_do = [
        s for s in template.get("sections", [])
        if only_sections is None or s["key"] in only_sections
    ]

    result = DraftResult(
        case_number=pack.case_number,
        provider=llm.name,
        model=getattr(llm, "_model", ""),
    )
    started = time.perf_counter()

    for section in sections_to_do:
        # Резолютивная, «Доводы ответчика» и вводная — правилами, не моделью.
        if section["key"] in RULES_BUILT_SECTIONS:
            if section["key"] == "operative":
                result.sections.append(_build_operative_section(section, pack))
            elif section["key"] == "objections":
                result.sections.append(_build_objections_section(section, pack))
            elif section["key"] == "descriptive":
                result.sections.append(_build_descriptive_section(section, pack))
            else:
                result.sections.append(_build_intro_section(section, pack))
            continue

        request_text, citations = build_section_prompt(section, pack)

        # Две попытки. Первая — обычный запрос. Вторая — с якорным
        # началом: на живом прогоне «УСТАНОВИЛ» приходил английским
        # рассуждением и обрывался ровно на лимите токенов, а
        # «Мотивировочная» — описанием работы модели («Хорошо, давайте
        # разберемся…»). Якорная фраза держит ответ в русском тексте с
        # первой буквы, поэтому лимит на второй попытке поднят.
        prompts = [request_text]
        anchor = SECTION_ANCHORS.get(section["key"], "")
        if anchor:
            prompts.append(
                f"{request_text}\n\nНачни текст ровно с фразы «{anchor}».\n"
                "Дальше — только текст раздела: без пояснений, планов и "
                "описания того, что ты делаешь. Пиши по-русски, "
                "официально-деловым стилём."
            )

        text = ""
        needs_review = False
        is_mock = False
        error = ""
        gaps: list[str] = []
        latency_ms = 0
        reason = ""

        for attempt, prompt in enumerate(prompts):
            request = LLMRequest(
                messages=[
                    LLMMessage(
                        role="system",
                        content=SYSTEM_PROMPT.format(
                            review=NEEDS_REVIEW, no_law=NO_LAW_FOUND
                        ),
                    ),
                    LLMMessage(role="user", content=prompt),
                ],
                role=LLMRole.GENERATION,
                # Низкая температура: нужна работа с фактами, а не свободное письмо.
                temperature=0.1,
                # Раздел решения — не кандидатская, длинный текст роняет
                # качество на маленькой модели и растягивает время на CPU.
                # 420 токенов — предел для 1.5B на CPU: дальше качество
                # падает быстрее, чем растёт время генерации.
                max_tokens=420 if attempt == 0 else 760,
            )
            started_section = time.perf_counter()
            try:
                if cache is None:
                    response = await llm.generate(request)
                else:
                    response = await cache.generate(
                        llm,
                        request,
                        task=(
                            TaskKind.SECTION_DRAFT
                            if attempt == 0
                            else TaskKind.SECTION_RETRY
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Раздел %s не сгенерирован: %s", section["key"], exc)
                error = str(exc)[:200]
                # Сбой самой модели (Ollama уронила процесс, оборвалось
                # соединение) — не приговор разделу: вторая попытка с
                # якорем идёт тем же путём. Если и она упадёт, раздел
                # остаётся несформированным и честно помечается.
                if attempt + 1 < len(prompts):
                    continue
                break
            latency_ms += int((time.perf_counter() - started_section) * 1000)

            # Ответ модели может оказаться не текстом раздела, а её
            # рассуждениями или повтором одной фразы. Такой текст не
            # идёт в проект: сначала даётся вторая попытка с якорем,
            # а если и она не удалась — раздел остаётся несформированным
            # и попадает в блок «Не хватает данных».
            candidate, candidate_review = _postprocess(response.text, section)
            bad = unusable_reason(candidate)
            if not bad:
                # Суммы из текста сверяются с материалами до того, как
                # раздел попадёт в проект. Выдуманное число нельзя
                # показывать судье без пометки: на живом прогоне модель
                # дописала 6 470 руб. расходов на представителя, которых
                # в деле не было ни в одном документе.
                marked, unsourced = mark_unsourced_amounts(
                    candidate,
                    pack.documents.values(),
                    pack.amounts,
                )
                text = marked
                needs_review = candidate_review or bool(unsourced)
                is_mock = response.is_mock
                error = ""
                for amount in unsourced:
                    gaps.append(
                        f"сумма {amount.raw} руб. не найдена в материалах дела"
                    )
                # Сумма может быть в деле, но относиться к другому
                # требованию («проценты 80 000» вместо «долг 80 000»).
                # Проверка «число встречается» такое пропускает, поэтому
                # назначение сверяется отдельно.
                wrong_purpose = [
                    finding
                    for finding in check_amounts_by_purpose(
                        candidate, pack.documents.values()
                    )
                    if finding.check_id == "AMOUNT_WRONG_PURPOSE"
                ]
                for finding in wrong_purpose:
                    gaps.append(finding.reason)
                needs_review = needs_review or bool(wrong_purpose)
                break
            reason = bad
            logger.info(
                "Раздел %s (попытка %d) отклонён: %s",
                section["key"],
                attempt + 1,
                bad,
            )

        if not text:
            # Раздел не сформирован: в тексте пусто, причина — в блоке
            # пробелов. Верификация по полю error поднимает это до
            # CRITICAL — в акте дыра, а не предупреждение.
            detail = error or reason or "модель не вернула текст раздела"
            if not error:
                error = detail
            gaps.append(f"раздел не сформирован: {detail}")

        result.sections.append(
            SectionDraft(
                key=section["key"],
                name=section["name"],
                text=text,
                is_mock=is_mock,
                citations=citations,
                needs_review=needs_review or bool(gaps),
                latency_ms=latency_ms,
                error=error,
                gaps=gaps,
            )
        )

    # Предмет доказывания по виду договора: обстоятельства, которых нет
    # ни в одном документе дела, уходят в блок «Не хватает данных»
    # мотивировочной части. Судья обязан видеть, чего в деле не хватает
    # для вывода, а не читать в акте фразу о «совокупности
    # доказательств» — по одной таблице на вид договора (см.
    # app.verification.subject_of_proof).
    uncovered = proof_gaps(pack.items)
    if uncovered:
        for section in result.sections:
            if section.key == "motivating":
                section.gaps.extend(uncovered)
                section.needs_review = True
                break

    # Пометки о нехватке данных не должны оставаться в тексте акта —
    # они уходят в блок «Не хватает данных» (см. _strip_markers).
    for section in result.sections:
        _strip_markers(section)

    # Модель на «УСТАНОВИЛ» отвечает заголовком, который после обработки
    # становится пустым текстом. Пустой раздел в акте — дыра, поэтому
    # подставляются обстоятельства из материалов. Проверяется уже
    # очищенный текст: иначе заглушка проходит проверку и остаётся.
    for position, section in enumerate(result.sections):
        if (
            section.key == "established"
            and not section.text.strip()
            # Технический отказ модели (LLM недоступен) — это не «модель
            # ответила заголовком»: тут честнее пустой раздел с пометкой,
            # чем текст, собранный правилами вместо недоступной модели.
            and not section.error
        ):
            built = _build_established_section(
                {"key": section.key, "name": section.name}, pack
            )
            reason_text = (
                section.gaps[0] if section.gaps else "модель не дала текста"
            )
            built.gaps = [
                f"раздел собран по материалам: {reason_text}",
                *built.gaps,
            ]
            built.latency_ms = section.latency_ms
            result.sections[position] = built

    result.total_latency_ms = int((time.perf_counter() - started) * 1000)
    return result
