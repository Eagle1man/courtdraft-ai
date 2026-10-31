"""Нормативные ссылки: существование и применимость — разные проверки.

Этап 5 ТЗ. Главное правило: существование статьи не доказывает её
применимость, а наличие цитаты — правильность квалификации. Поэтому
здесь два независимых шага:

1. `check_existence` — есть ли такая статья в базе законодательства и
   действует ли она на дату.
2. `check_applicability` — применима ли норма к обстоятельствам дела
   (делегируется `forbidden_refs`, где правила по виду договора).

Проверка не решает за судью: она показывает, что именно не подтверждено.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.verification.forbidden_refs import check_forbidden_refs
from app.verification.laws import LawRegistry

#: «п. 4 ч. 1 ст. 23 ГПК РФ», «ст. 309 ГК РФ», «ст. 333 ГК РФ».
_REF_RE = re.compile(
    r"(?:п(?:\.|ункт)\s*(?P<point>\d+(?:\.\d+)?)\s*)?"
    r"(?:ч(?:\.|асть)\s*(?P<part>\d+)\s*)?"
    r"ст(?:\.|атья)\s*(?P<article>\d+(?:\.\d+)?)\s*"
    r"(?P<code>ГК|ГПК|НК|КАС|АПК|ТК|СК)\s*РФ",
    re.IGNORECASE,
)

_CODES = {
    "ГК": "ГК", "ГПК": "ГПК", "НК": "НК", "КАС": "КАС",
    "АПК": "АПК", "ТК": "ТК", "СК": "СК",
}


@dataclass(frozen=True)
class LegalRef:
    """Разобранная ссылка на норму."""

    code: str
    article: str
    part: str = ""
    point: str = ""

    def label(self) -> str:
        pieces: list[str] = []
        if self.point:
            pieces.append(f"п. {self.point}")
        if self.part:
            pieces.append(f"ч. {self.part}")
        pieces.append(f"ст. {self.article}")
        pieces.append(f"{self.code} РФ")
        return " ".join(pieces)


@dataclass(frozen=True)
class LegalFinding:
    """Результат проверки нормативной ссылки."""

    check_id: str  # LAW_NOT_FOUND | LAW_NOT_IN_EFFECT | LAW_NOT_APPLICABLE
    severity: str
    reason: str
    fragment: str = ""


def parse_refs(text: str) -> list[LegalRef]:
    """Выделить нормативные ссылки из текста."""
    refs: list[LegalRef] = []
    for match in _REF_RE.finditer(text or ""):
        code = _CODES.get(match.group("code").upper())
        if code is None:
            continue
        refs.append(
            LegalRef(
                code=code,
                article=match.group("article"),
                part=match.group("part") or "",
                point=match.group("point") or "",
            )
        )
    return refs


def check_existence(
    text: str,
    laws_dir: Path,
    *,
    on_date: date | None = None,
) -> list[LegalFinding]:
    """Существует ли норма и действует ли она на дату."""
    registry = LawRegistry.load(laws_dir)
    findings: list[LegalFinding] = []
    seen: set[tuple[str, str]] = set()

    for ref in parse_refs(text):
        key = (ref.code, ref.article)
        if key in seen:
            continue
        seen.add(key)

        if not registry.contains(ref.code, ref.article):
            findings.append(
                LegalFinding(
                    check_id="LAW_NOT_FOUND",
                    severity="HIGH",
                    reason=f"{ref.label()}: такой статьи нет в базе законодательства",
                    fragment=ref.label(),
                )
            )
            continue

        if not registry.in_force(ref.code, ref.article, on_date):
            findings.append(
                LegalFinding(
                    check_id="LAW_NOT_IN_EFFECT",
                    severity="HIGH",
                    reason=registry.missing(ref.code, ref.article),
                    fragment=ref.label(),
                )
            )
    return findings


def check_applicability(
    text: str, *, supply: bool = False
) -> list[LegalFinding]:
    """Применима ли норма к обстоятельствам дела.

    Существование проверено отдельно: здесь только несоответствие
    нормы виду спора.
    """
    findings: list[LegalFinding] = []
    for item in check_forbidden_refs(text, supply=supply):
        findings.append(
            LegalFinding(
                check_id="LAW_NOT_APPLICABLE",
                severity="HIGH",
                reason=getattr(item, "reason", str(item)),
                fragment=getattr(item, "fragment", ""),
            )
        )
    return findings
