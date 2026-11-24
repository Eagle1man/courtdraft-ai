"""Классификация документов: сначала правила, LLM — при неоднозначности."""

from __future__ import annotations

import re
from typing import Any

from .models import DocClass, DocIntake, Fragment

DOC_TYPES = [
    "contract",
    "supplementary_agreement",
    "act",
    "invoice",
    "payment",
    "pretension",
    "pretension_response",
    "notice",
    "correspondence",
    "power_of_attorney",
    "statement",
    "court_document",
    "claim_statement",
    "response_statement",
    "party_card",
    "explanations",
    "calculation",
    "attachment_list",
    "enclosure",
    "fee_receipt",
    "other",
    "unknown",
]

RULES: list[tuple[str, tuple[str, ...]]] = [
    ("claim_statement", (r"исковое\s+заявление", r"прошу\s+взыскать")),
    ("response_statement", (r"отзыв\s+на\s+исков", r"возражения\s+на\s+иск")),
    ("pretension_response", (r"ответ\s+на\s+претензи",)),
    ("pretension", (r"претензи[яи]",)),
    ("power_of_attorney", (r"доверенност[ьи]",)),
    ("court_document", (r"решение\s+именем", r"определение\s+суда", r"суд\s+решил")),
    ("supplementary_agreement", (r"дополнительное\s+соглашение",)),
    ("contract", (r"договор\s+№", r"договор\s+займа", r"договор\b")),
    ("act", (r"^\s*акт\b", r"акт\s+(выполненных|сверки|приема)")),
    ("invoice", (r"счет[а]?\s+на\s+оплату", r"инвойс")),
    (
        "payment",
        (
            r"платежное\s+поручение",
            r"платёжное\s+поручение",
            r"квитанци[яи]",
            r"кассовый\s+чек",
        ),
    ),
    ("statement", (r"выписк[аи]\s+из",)),
    ("notice", (r"уведомлени[ея]",)),
    ("correspondence", (r"письм[оа]",)),
]


def classify_by_rules(doc: DocIntake, frags: list[Fragment]) -> DocClass | None:
    head = "\n".join(fragment.text for fragment in frags[:6]).lower()
    name = doc.filename.lower()
    filename_rules = [
        ("party_card", ("карточка",)),
        ("explanations", ("пояснения",)),
        ("receipt", ("расписк",)),
        ("calculation", ("расчет", "расчёт")),
        ("attachment_list", ("опись приложений",)),
        ("enclosure", ("опись вложения",)),
        ("fee_receipt", ("чек-ордер", "чек ордер")),
        ("claim_statement", ("исковое заявление",)),
    ]
    for doc_type, markers in filename_rules:
        if any(marker in name for marker in markers):
            return DocClass(
                doc_type=doc_type,
                confidence=0.95,
                reasons=["тип определён по имени файла"],
            )
    hits: list[tuple[int, str]] = []
    for doc_type, patterns in RULES:
        score = sum(1 for pattern in patterns if re.search(pattern, head, re.I))
        score += sum(1 for pattern in patterns if re.search(pattern, name, re.I))
        if score:
            hits.append((score, doc_type))
    hits.sort(reverse=True)
    if not hits:
        return None
    best = hits[0]
    second = hits[1] if len(hits) > 1 else (0, "")
    confidence = 0.55 + 0.15 * best[0]
    if second[0] >= best[0]:
        return DocClass(
            doc_type="unknown",
            confidence=0.3,
            reasons=[f"равные признаки: {best[1]} и {second[1]}"],
            alternatives=[best[1], second[1]],
        )
    return DocClass(
        doc_type=best[1],
        confidence=min(confidence, 0.9),
        reasons=["совпадение с правилами по заголовку/имени файла"],
        alternatives=[second[1]] if second[1] else [],
    )


CLASSIFY_SYSTEM = (
    "Ты классификатор юридических документов. Верни строго JSON: "
    '{"doc_type": один из списка, "confidence": 0..1, "reasons": [..], '
    '"alternatives": [..]}. Если оснований недостаточно — '
    '{"doc_type":"unknown","confidence":0,"reasons":["недостаточно оснований"]}. '
    "Выдумывать тип запрещено."
)


async def classify_doc(llm: Any, doc: DocIntake, frags: list[Fragment]) -> DocClass:
    result = classify_by_rules(doc, frags)
    if result is not None and result.doc_type != "unknown":
        return result
    text = "\n".join(fragment.text for fragment in frags[:8])[:4000]
    try:
        answer = await llm.generate_json(
            CLASSIFY_SYSTEM,
            f"Типы: {', '.join(DOC_TYPES)}\nИмя файла: {doc.filename}\nТекст:\n{text}",
        )
        doc_type = str(answer.get("doc_type", "unknown"))
        return DocClass(
            doc_type=doc_type if doc_type in DOC_TYPES else "unknown",
            confidence=float(answer.get("confidence", 0) or 0),
            reasons=[str(item) for item in answer.get("reasons", [])][:4],
            alternatives=[str(item) for item in answer.get("alternatives", [])][:3],
            by_llm=True,
        )
    except Exception as exc:
        return DocClass(
            doc_type="unknown",
            confidence=0.0,
            reasons=[f"LLM недоступна: {type(exc).__name__}"],
            alternatives=([result.alternatives[0]] if result and result.alternatives else []),
        )
