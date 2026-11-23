"""Генерация и проверка искового заявления (фаза 1).

Фокус: facts, request, calculation, legal_basis. Детерминированные разделы
(шапка, стороны, цена/пошлина, приложения, подпись) собираются из данных
конструктора и материалов дела; факты и правовое обоснование могут
дополняться моделью, но только с проверками:
- запрещены формулировки судебного акта;
- нормы и суммы без источника помечаются [требует проверки];
- ссылки на нормы принимаются только при наличии подтверждения в контексте.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agents.draft_agent import (
    NEEDS_REVIEW,
    DraftResult,
    LLMMessage,
    LLMRequest,
    LLMRole,
    SectionDraft,
    _postprocess,
)
from app.config import Settings, get_settings
from app.drafting.claim_builder import ClaimBuildInput, build_claim
from app.llm import build_llm_provider
from app.llm.base import LLMProvider
from app.rag.retrieval import BM25Index, Document
from app.verification.engine import Issue, IssueType, Severity, VerificationReport, VerificationStatus

logger = logging.getLogger(__name__)

FORBIDDEN_JUDGMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("суд установил", re.compile(r"\bсуд(?:ом)?\s+установ", re.IGNORECASE)),
    ("суд приходит к выводу", re.compile(r"\bсуд(?:ом)?\s+при[хш][оё]д\w*\s+к\s+вывод", re.IGNORECASE)),
    ("суд считает", re.compile(r"\bсуд(?:ом)?\s+счита", re.IGNORECASE)),
    ("суд постановил/решил", re.compile(r"\bсуд(?:ом)?\s+(?:постанов|решил|определил)", re.IGNORECASE)),
    ("ст. 198 ГПК", re.compile(r"\bст(?:атьи|\.)?\s*198\s*ГПК", re.IGNORECASE)),
    ("именем Российской Федерации", re.compile(r"именем\s+Российской\s+Федерации", re.IGNORECASE)),
    ("резолютивная часть", re.compile(r"\bрезолютивн\w*\s+част", re.IGNORECASE)),
)

CLAIM_SECTION_ORDER = [
    "header",
    "parties",
    "price",
    "facts",
    "legal_basis",
    "calculation",
    "request",
    "attachments",
    "signature",
]


def _claim_input_from_context(claim_data: dict[str, Any] | None, case: Any = None) -> ClaimBuildInput:
    data = dict(claim_data or {})
    if case is not None:
        if not str(data.get("court_name") or "").strip() and getattr(case, "court_name", ""):
            data["court_name"] = case.court_name
        if not str(data.get("category") or "").strip() and getattr(case, "category", ""):
            data["category"] = case.category
    return ClaimBuildInput.from_dict(data)


def _deterministic_sections(
    template: dict[str, Any], pack: Any, claim_data: dict[str, Any] | None, case: Any = None
) -> tuple[dict[str, SectionDraft], dict[str, Any]]:
    claim_input = _claim_input_from_context(claim_data, case)
    built = build_claim(claim_input)
    price_text = f"Цена иска: {built.get('claim_price') or 'не определена'} руб."
    if built.get("state_fee"):
        fee = built["state_fee"]
        price_text += (
            f"\nГосударственная пошлина (расчёт): {fee.get('amount_rub') or fee.get('amount')} руб."
            f"\nОснование: {fee.get('basis', '')}\n"
            "Размер рассчитан автоматически, льготы и подтверждение уплаты требуют проверки."
        )
    else:
        price_text += "\nГосударственная пошлина: [требует проверки]"

    facts_lines = []
    for f in claim_input.facts:
        prefix = f"{f.date}: " if f.date else "- "
        src = f" [источник: {f.source_document}]" if f.source_document else f" [{NEEDS_REVIEW}: источник]"
        facts_lines.append(prefix + (f.text or f"[{NEEDS_REVIEW}: обстоятельство]") + src)
    facts_text = "\n".join(facts_lines) if facts_lines else f"[{NEEDS_REVIEW}: обстоятельства не указаны]"

    calc_text = _calculation_text(claim_input, built)
    req_text = _request_text(claim_input)
    attach_text = _attachments_text(claim_input, built)
    parties_text = _parties_text(claim_input)
    header_text = claim_input.court_name or "[требует проверки: наименование суда]"
    if claim_input.court_address:
        header_text += f"\n{claim_input.court_address}"
    signature_text = "Дата: «___» __________ ____ г.\nПодпись истца (представителя): ____________ / __________________ /"

    mapping = {
        "header": header_text,
        "parties": parties_text,
        "price": price_text,
        "facts": facts_text,
        "legal_basis": f"[{NEEDS_REVIEW}: правовое обоснование требует подбора подтверждённых норм по материалам дела]",
        "calculation": calc_text,
        "request": req_text,
        "attachments": attach_text,
        "signature": signature_text,
    }

    # build by template sections if present, else default order
    sections_def = template.get("sections") or [{"key": k, "name": k} for k in CLAIM_SECTION_ORDER]
    out: dict[str, SectionDraft] = {}
    for sec in sections_def:
        key = sec.get("key", "")
        name = sec.get("name", key)
        text = mapping.get(key, f"[{NEEDS_REVIEW}: раздел не сформирован]")
        needs_review = NEEDS_REVIEW in text or "требует проверки" in text.lower()
        gaps = []
        if key == "facts" and not claim_input.facts:
            gaps.append("обстоятельства не указаны")
        if key == "calculation" and not any(getattr(d, "amount", None) is not None for d in claim_input.demands):
            gaps.append("расчёт не подтверждён суммами")
        if key == "legal_basis":
            gaps.append("нормы права не подтверждены источниками")
        out[key] = SectionDraft(
            key=key,
            name=name,
            text=text,
            is_mock=False,
            needs_review=needs_review or bool(gaps),
            built_by="rules",
            gaps=gaps,
        )
    return out, built


def _parties_text(data: ClaimBuildInput) -> str:
    def fmt(label: str, p: Any) -> str:
        name = p.name or f"[{NEEDS_REVIEW}: {label}]"
        addr = p.address or f"[{NEEDS_REVIEW}: адрес]"
        lines = [f"{label}: {name}", f"Адрес/место нахождения: {addr}"]
        if p.identifiers:
            lines.append(f"Реквизиты: {p.identifiers}")
        if p.representative:
            lines.append(f"Представитель: {p.representative}")
        return "\n".join(lines)

    parts = [fmt("Истец", data.plaintiff), "", fmt("Ответчик", data.defendant)]
    for i, tp in enumerate(data.third_parties, start=1):
        parts.extend(["", fmt(f"Третье лицо {i}", tp)])
    return "\n".join(parts)


def _calculation_text(data: ClaimBuildInput, built: dict[str, Any]) -> str:
    lines = []
    total = 0.0
    has_money = False
    for d in data.demands:
        amt = d.amount
        try:
            val = float(str(amt).replace(" ", "").replace(",", ".")) if amt is not None and str(amt).strip() != "" else None
        except Exception:
            val = None
        label = d.description or d.kind
        if val is not None:
            has_money = True
            total += val
            extra = []
            if d.period_from or d.period_to:
                extra.append(f"период {d.period_from or '…'} — {d.period_to or '…'}")
            if d.basis:
                extra.append(d.basis)
            tail = f" ({'; '.join(extra)})" if extra else ""
            # Сумма должна быть подтверждена документом/расчётом; без указания основания/источника — пометка проверки.
            src = "" if d.basis else " [требует проверки: подтвердите сумму документом]"
            lines.append(f"- {label}: {val:g} руб.{tail}{src}")
        else:
            lines.append(f"- {label}: [{NEEDS_REVIEW}: сумма]")
    if has_money:
        lines.append(f"Итого: {total:g} руб.")
    if not lines:
        return f"[{NEEDS_REVIEW}: расчёт не может быть составлен без сумм и исходных данных]"
    lines.append("")
    lines.append("Примечание: расчёт основан на введённых данных и подлежит проверке по документам.")
    return "\n".join(lines)


def _request_text(data: ClaimBuildInput) -> str:
    if not data.demands:
        return f"1. [{NEEDS_REVIEW}: сформулируйте требование]"
    lines = []
    for i, d in enumerate(data.demands, start=1):
        amt = d.amount
        try:
            val = float(str(amt).replace(" ", "").replace(",", ".")) if amt is not None and str(amt).strip() != "" else None
        except Exception:
            val = None
        desc = (d.description or "").strip()
        if val is not None and desc:
            req = desc if str(int(val)) in desc.replace(" ", "") or f"{val:g}" in desc else f"{desc} в размере {val:g} руб."
        elif val is not None:
            req = f"Взыскать с ответчика в пользу истца {val:g} руб."
        else:
            req = desc or f"[{NEEDS_REVIEW}: требование]"
        # Если сумма не подкреплена основанием/источником в данных — явно помечаем необходимость проверки.
        if val is not None and not d.basis:
            req += " [требует проверки: подтвердите сумму документом]"
        lines.append(f"{i}. {req}")
    return "\n".join(lines)


def _attachments_text(data: ClaimBuildInput, built: dict[str, Any]) -> str:
    provided = [a for a in data.attachments if a.name]
    if provided:
        return "\n".join(
            f"{i}. {a.name}" + (f" — {a.copies} экз." if a.copies else "") for i, a in enumerate(provided, start=1)
        )
    rec = (built.get("attachments") or {}).get("recommended") or []
    if rec:
        return "\n".join(f"{i}. {a.get('name','')}" for i, a in enumerate(rec, start=1))
    return f"1. [{NEEDS_REVIEW}: перечень приложений]"


def _collect_forbidden(text: str) -> list[str]:
    found = []
    for label, pat in FORBIDDEN_JUDGMENT_PATTERNS:
        if pat.search(text or ""):
            found.append(label)
    return found


def _strip_forbidden(text: str) -> tuple[str, list[str]]:
    found = _collect_forbidden(text)
    cleaned = text
    for label, pat in FORBIDDEN_JUDGMENT_PATTERNS:
        if pat.search(cleaned):
            # Replace whole sentence fragments conservatively by marker
            cleaned = pat.sub("[недопустимая формулировка судебного акта удалена]", cleaned)
    return cleaned, found


#: Лимиты материалов под жёсткий context_length=2048 у Ollama-модели.
_MATERIALS_BUDGET = 1200
_MATERIALS_DOC_LIMIT = 4
_MATERIALS_DOC_CHARS = 450
_MATERIALS_CHUNK_CHARS = 900


def _bm25_select_documents(
    documents: dict[str, str],
    name: str,
    purpose: str,
    claim_data: dict[str, Any] | None,
) -> list[str]:
    """Отобрать релевантные файлы из большого набора (сценарий «400 файлов»).

    Первые попавшиеся документы при массовой загрузке почти наверняка не
    относятся к разделу. Строим локальный BM25-индекс по кускам документов
    (кусок важен, а не начало файла-шапки) и берём файлы с лучшим совпадением
    с названием раздела, назначением и данными конструктора.
    """
    try:
        index = BM25Index()
        for fname, text in documents.items():
            body = (text or "").strip()
            if not body:
                continue
            for offset in range(0, len(body), _MATERIALS_CHUNK_CHARS):
                index.add(
                    Document(
                        chunk_id=f"{fname}#{offset}",
                        text=body[offset : offset + _MATERIALS_CHUNK_CHARS],
                        filename=fname,
                    )
                )
        query = " ".join(
            p for p in (name, purpose, str(claim_data or "")[:400]) if p
        )
        results = index.search(query, limit=_MATERIALS_DOC_LIMIT)
        picked: list[str] = []
        seen: set[str] = set()
        for r in results:
            fname = r.embedding.filename or r.embedding.chunk_id.split("#", 1)[0]
            if fname in seen or fname not in documents:
                continue
            seen.add(fname)
            picked.append(fname)
            if len(picked) >= _MATERIALS_DOC_LIMIT:
                break
        if picked:
            return picked
    except Exception:
        logger.warning(
            "claim draft: BM25-отбор материалов не удался, берём первые файлы",
            exc_info=True,
        )
    return list(documents.keys())[:_MATERIALS_DOC_LIMIT]


def _select_materials(
    pack: Any,
    name: str,
    purpose: str,
    claim_data: dict[str, Any] | None,
) -> str:
    """Вернуть материалы дела в лимит символов (детерминированно)."""
    try:
        raw = dict(getattr(pack, "documents", {}) or {})
    except Exception:
        raw = {}
    documents = {n: (t or "") for n, t in raw.items() if (t or "").strip()}
    if not documents:
        return "Материалы дела отсутствуют."

    names = list(documents.keys())
    if len(names) <= _MATERIALS_DOC_LIMIT:
        selected = names[:_MATERIALS_DOC_LIMIT]
    else:
        selected = _bm25_select_documents(documents, name, purpose, claim_data)

    blocks = [
        f"[{n}]\n{(documents.get(n) or '')[:_MATERIALS_DOC_CHARS]}"
        for n in selected
    ]
    materials = "\n\n".join(blocks) or "Материалы дела отсутствуют."
    if len(materials) > _MATERIALS_BUDGET:
        materials = materials[:_MATERIALS_BUDGET] + " …[материал сокращён под лимит контекста]"
    return materials


#: Затравки для второй попыт («допиши текст»). Это структура, а не факт:
#: модель получает начало фразы и продолжает её по материалам.
_SECTION_ANCHORS: dict[str, str] = {
    "legal_basis": "Требования истца основаны на следующих нормах:",
    "request": "На основании изложенного истец просит:",
    "calculation": "Расчёт суммы требований:",
    "facts": "Истец указывает следующие обстоятельства:",
}

#: Признаки того, что модель пересказала саму «допиши»-инструкцию.
_RETRY_ECHO_MARKERS: tuple[str, ...] = (
    "не пересказывай",
    "продолжи его",
    "начало текста:",
    "продолжение:",
    "только для сведений",
)


def _is_retry_echo(text: str) -> bool:
    low = (text or "").lower()
    return sum(1 for m in _RETRY_ECHO_MARKERS if m in low) >= 2


async def _retry_generate_section(
    provider: LLMProvider,
    system: str,
    key: str,
    name: str,
    purpose: str,
    materials: str,
    laws: str,
    claim_data: dict[str, Any] | None,
    settings: Settings,
) -> tuple[str, bool, list[str], bool, str, int, int] | None:
    """Вторая попытка: «допиши продолжение» вместо пересказа задания.

    Возвращает уже очищенный текст и метаданные, либо None, если повтор
    тоже ушёл в эхо/не удался. Первый промпт («Раздел: … Назначение: …
    Входные данные конструктора (JSON): …») 7B-модель пересказывает
    дословно; здесь пересказывать нечего — есть только затравка и материалы.
    """
    anchor = _SECTION_ANCHORS.get(key, f"Истец указывает по разделу «{name}»:")
    retry_user = (
        f"Текст раздела «{name}» уже начат. Продолжи его следующим "
        f"предложением, не пересказывай задание и не повторяй материалы.\n\n"
        f"Начало текста: {anchor}\n\n"
        f"Материалы (только для сведений, не пересказывай):\n<<<\n{materials}\n>>>\n\n"
        f"Нормы (только для сведений, не пересказывай):\n<<<\n{laws}\n>>>\n\n"
        f"Данные конструктора: {str(claim_data or {})[:500]}\n\n"
        f"Продолжение:"
    )
    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=retry_user),
        ],
        role=LLMRole.GENERATION,
        temperature=0.1,
        max_tokens=getattr(settings, "draft_max_tokens", 900),
    )
    try:
        response = await provider.generate(request)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "claim draft: повторная генерация '%s' не удалась: %s", key, type(exc).__name__
        )
        return None
    text = response.text or ""
    cleaned, post_review = _postprocess(text, {"name": name, "key": key})
    cleaned, forbidden = _strip_forbidden(cleaned)
    if not cleaned or _is_prompt_echo(cleaned) or _is_retry_echo(text):
        return None
    usage = getattr(response, "usage", None)
    pt = getattr(usage, "prompt_tokens", 0) if usage else 0
    ct = getattr(usage, "completion_tokens", 0) if usage else 0
    return (
        cleaned,
        post_review,
        forbidden,
        getattr(response, "is_mock", False),
        getattr(response, "model", "") or "",
        pt,
        ct,
    )


async def _generate_text_section(
    key: str,
    name: str,
    pack: Any,
    provider: LLMProvider,
    claim_data: dict[str, Any] | None,
    purpose: str,
) -> SectionDraft:
    settings = get_settings()
    # Контекст модели в Ollama жёстко ограничен2048 токенами (context length
    # в GGUF): num_ctx=4096 и 8192 Ollama не принимает — запрос длиннее 2048
    # токенов отклоняется с HTTP 400 «exceeds the available context size».
    # Вместе с ответом (draft_max_tokens=420) под промпт остаётся ~1600 токенов,
    # то есть ~4500 знаков кириллицы (≈3 знака на токен). Держим суммарно
    # ~3800 знаков с запасом: раздел без материала — это лучше, чем упавший
    # вызов модели.
    materials = _select_materials(pack, name, purpose, claim_data)
    law_blocks = []
    try:
        for b in (getattr(pack, "law_context", None) or [])[:4]:
            if b and not str(b).startswith("[TRACE_LEDGER_SUMMARY]"):
                law_blocks.append(str(b)[:400])
    except Exception:
        pass
    laws = "\n\n".join(law_blocks) if law_blocks else "Подтверждённые тексты норм не предоставлены."
    if len(laws) > 1200:
        laws = laws[:1200] + " …[правовой контекст сокращён под лимит контекста]"

    system = (
        "Ты помогаешь подготовить черновик ИСКОВОГО ЗАЯВЛЕНИЯ (сторона истца), а не судебный акт. "
        "Пиши только по материалам и входным данным. Не выдумывай факты, суммы, даты, реквизиты и нормы. "
        "Запрещено писать как суд: нельзя 'суд установил', 'суд приходит к выводу', 'суд считает', 'именем Российской Федерации', "
        "нельзя ссылаться на ст. 198 ГПК РФ и оформлять резолютивную часть. "
        "Каждую сумму, дату и норму указывай только при наличии источника в материалах/контексте; иначе ставь [требует проверки]. "
        "Ссылайся на нормы права только если они подтверждены в предоставленном правовом контексте; неподтверждённые нормы не утверждай."
    )
    user = (
        f"Раздел: {name}\nНазначение: {purpose}\n"
        f"Входные данные конструктора (JSON):\n{str(claim_data or {})[:700]}\n\n"
        f"Материалы дела:\n<<<\n{materials}\n>>>\n\n"
        f"Правовой контекст (только подтверждённые источники):\n<<<\n{laws}\n>>>\n\n"
        f"Сформируй текст раздела. Если данных недостаточно — явно укажи [{NEEDS_REVIEW}]."
    )
    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=user),
        ],
        role=LLMRole.GENERATION,
        temperature=0.1,
        max_tokens=getattr(settings, "draft_max_tokens", 900),
    )
    try:
        response = await provider.generate(request)
        text = response.text or ""
        is_mock = getattr(response, "is_mock", False)
        model = getattr(response, "model", "") or ""
        usage = getattr(response, "usage", None)
        pt = getattr(usage, "prompt_tokens", 0) if usage else 0
        ct = getattr(usage, "completion_tokens", 0) if usage else 0
    except Exception as exc:  # noqa: BLE001
        return SectionDraft(
            key=key,
            name=name,
            text=f"[{NEEDS_REVIEW}: раздел не сформирован автоматически ({type(exc).__name__})]",
            is_mock=False,
            needs_review=True,
            built_by="rules",
            gaps=[f"ошибка генерации: {str(exc)[:120]}"],
        )

    cleaned, post_review = _postprocess(text or "", {"name": name, "key": key})
    cleaned, forbidden = _strip_forbidden(cleaned)

    # Модель пересказала задание — это не текст раздела. Сначала пробуем
    # вторую попытку в формате «допиши» (там пересказывать нечего); только
    # если и она вернула эхо — детерминированный текст по введённым данным.
    if _is_prompt_echo(cleaned):
        retry = await _retry_generate_section(
            provider, system, key, name, purpose, materials, laws, claim_data, settings
        )
        if retry is not None:
            (
                cleaned,
                post_review,
                forbidden,
                is_mock,
                model,
                pt,
                ct,
            ) = retry
        else:
            return SectionDraft(
                key=key,
                name=name,
                text=_fallback_section_text(key, claim_data, law_blocks),
                is_mock=is_mock,
                needs_review=True,
                built_by="rules",
                gaps=[
                    "модель вернула пересказ задания вместо текста раздела — "
                    "раздел собран по введённым данным"
                ],
                model=model,
                prompt_tokens=pt or 0,
                completion_tokens=ct or 0,
            )

    needs_review = post_review or (NEEDS_REVIEW in cleaned) or bool(forbidden) or ("требует проверки" in cleaned.lower())
    gaps = []
    if forbidden:
        needs_review = True
        gaps.append("удалены формулировки судебного акта: " + ", ".join(forbidden))
    if key == "legal_basis":
        if not law_blocks:
            needs_review = True
            gaps.append("нет подтверждённого правового контекста — нормы требуют проверки")
            if "требует проверки" not in cleaned.lower():
                cleaned += f"\n\n[{NEEDS_REVIEW}: нормы права не подтверждены источниками]"
        else:
            # KB-only: если есть ссылки на нормы, но подтверждений в контексте нет — требуем проверку.
            cites = re.findall(r"\bст(?:атьи|\.|\s)?\s*\d+(?:\.\d+)?", cleaned or "", re.IGNORECASE)
            if cites:
                ctx_text = "\n".join(law_blocks)
                unconfirmed = [c for c in cites if re.sub(r"\s+", " ", c.lower()) not in re.sub(r"\s+", " ", ctx_text.lower())]
                if unconfirmed:
                    needs_review = True
                    gaps.append("есть ссылки на нормы без явного подтверждения в правовом контексте")
                    if "требует проверки" not in cleaned.lower():
                        cleaned += f"\n\n[{NEEDS_REVIEW}: подтвердите применимость норм по источникам]"
    if key in {"facts", "request", "calculation"}:
        has_amount = bool(re.search(r"\d[\d\s\u00a0]*([.,]\d{1,2})?\s*(?:руб|₽)", cleaned or "", re.IGNORECASE))
        if has_amount and "требует проверки" not in cleaned.lower() and "источник" not in cleaned.lower():
            # Суммы без явной привязки к источнику — помечаем.
            needs_review = True
            gaps.append("суммы без явной пометки источника/проверки")
            cleaned += f"\n[{NEEDS_REVIEW}: подтвердите суммы документами/расчётом]"
    return SectionDraft(
        key=key,
        name=name,
        text=cleaned,
        is_mock=is_mock,
        needs_review=needs_review,
        built_by="model",
        gaps=gaps,
        model=model,
        prompt_tokens=pt or 0,
        completion_tokens=ct or 0,
    )


async def generate_claim_draft(
    pack: Any,
    template: dict[str, Any],
    *,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
    case: Any = None,
    claim_data: dict[str, Any] | None = None,
) -> DraftResult:
    """Иск собирается детерминированно из паспорта дела. LLM не вызывается."""
    from app.agents.draft_agent import DraftResult, SectionDraft
    from app.drafting.claim_model import ClaimModel, ClaimModelError
    from app.drafting.claim_render import render_claim

    passport = _find_passport(pack)
    if passport is None:
        raise ClaimModelError(
            "в материалах нет 00-ПАСПОРТ-ДЕЛА.json (schema case-passport/3.1): "
            "детерминированный иск собрать не из чего"
        )
    model = ClaimModel.from_passport(passport)
    rendered = render_claim(model)
    sections = [
        SectionDraft(
            key=section.key,
            name=section.name,
            text=section.text,
            is_mock=False,
            built_by="rules",
            needs_review=False,
            citations=[],
            gaps=[],
        )
        for section in rendered.sections
    ]
    result = DraftResult(
        case_number=str(passport.get("case", {}).get("number", "")),
        sections=sections,
        provider="rules",
        model="",
    )
    result.text_override = rendered.text
    return result


def _find_passport(pack: Any) -> dict[str, Any] | None:
    import json
    for text in (getattr(pack, "documents", {}) or {}).values():
        body = (text or "").strip()
        if not body.startswith("{") or "case-passport" not in body[:200]:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        if str(data.get("schema", "")).startswith("case-passport/"):
            return data
    return None



_SANITIZED_MARKER = "недопустимая формулировка судебного акта удалена"

#: Признаки того, что модель пересказала задание вместо текста раздела.
#: Наблюдалось на 7B-модели: в раздел попадал сам промпт («Назначение: …,
#: Входные данные конструктора (JSON): …»). Юристу такой текст бесполезен,
#: а выглядит он как готовый раздел — поэтому это отдельный гейт.
_PROMPT_ECHO_MARKERS: tuple[str, ...] = (
    "входные данные конструктора",
    "назначение:",
    "правовой контекст",
    "сформируй текст раздела",
    "материалы дела:",
    "раздел:",
)


def _is_prompt_echo(text: str) -> bool:
    low = (text or "").lower()
    return sum(1 for marker in _PROMPT_ECHO_MARKERS if marker in low) >= 2


def _fallback_section_text(
    key: str, claim_data: dict[str, Any] | None, law_blocks: list[str]
) -> str:
    """Детерминированный текст раздела, когда модель непригодна."""
    if key == "facts":
        data = _claim_input_from_context(claim_data)
        lines: list[str] = []
        for f in data.facts:
            prefix = f"{f.date}: " if f.date else "- "
            src = (
                f" [источник: {f.source_document}]"
                if f.source_document
                else f" [{NEEDS_REVIEW}: источник]"
            )
            lines.append(prefix + (f.text or f"[{NEEDS_REVIEW}: обстоятельство]") + src)
        if lines:
            return "\n".join(lines)
        return f"[{NEEDS_REVIEW}: обстоятельства не указаны]"
    if key == "legal_basis":
        if law_blocks:
            return (
                f"[{NEEDS_REVIEW}: нормы права не подтверждены источниками — "
                "подберите и проверьте применимые нормы]"
            )
        return (
            f"[{NEEDS_REVIEW}: правовое обоснование не сформировано — "
            "нормы права не подтверждены источниками]"
        )
    return f"[{NEEDS_REVIEW}: раздел не сформирован автоматически]"


def verify_claim_draft(
    draft: DraftResult,
    pack: Any,
    claim_data: dict[str, Any] | None = None,
    *,
    passport: Any = None,
    mode: str = "draft",
) -> VerificationReport:
    """Проверка иска по модели и паспорту. Без паспорта документ блокируется."""
    from app.drafting.claim_model import ClaimModel
    from app.ingestion.passport import find_passport_in_pack
    from app.verification.claim_model_checks import verify_claim_model

    source = passport or find_passport_in_pack(pack)
    if source is None:
        report = VerificationReport(status=VerificationStatus.BLOCKED)
        report.issues.append(
            Issue(
                type=IssueType.INCOMPLETE,
                severity=Severity.CRITICAL,
                text="",
                reason=(
                    "X13: в материалах нет 00-ПАСПОРТ-ДЕЛА.json — "
                    "детерминированный иск не из чего собрать и не с чем сверить"
                ),
                section="claim",
            )
        )
        report.claim_proof_summary = {"p0_count": 1, "document_kind": "claim"}
        return report

    model = ClaimModel.from_passport(source.as_dict())
    text = draft.text_override or draft.text
    result = verify_claim_model(model, text, passport=source, mode=mode)
    return result.to_report(sections_checked=len(draft.sections) or 1)
