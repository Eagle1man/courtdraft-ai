"""Детерминированные guardrails для материалов и текста проекта.

Не заменяют юридическую проверку. Ловят то, что можно проверить кодом:
утечку судебного акта в материалы и служебные формулировки модели в тексте.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterable


@dataclass(frozen=True, slots=True)
class GuardrailFinding:
    code: str
    message: str
    fragment: str


_MATERIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("MATERIAL_LEAK", re.compile(r"\bсуд\s+решил\b", re.IGNORECASE)),
    ("MATERIAL_LEAK", re.compile(r"\bисковые\s+требования\b[^.]{0,80}\bудовлетворить\b", re.IGNORECASE)),
    ("MATERIAL_LEAK", re.compile(r"\bв\s+удовлетворении\b[^.]{0,80}\bотказать\b", re.IGNORECASE)),
    ("MATERIAL_LEAK", re.compile(r"\bприговор\b", re.IGNORECASE)),
)

_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FORBIDDEN_OUTPUT", re.compile(r"\bкак\s+(?:языковая|искусственная)\s+модель\b", re.IGNORECASE)),
    ("FORBIDDEN_OUTPUT", re.compile(r"\bсистемн\w*\s+промпт\w*", re.IGNORECASE)),
    ("FORBIDDEN_OUTPUT", re.compile(r"\bя\s+не\s+могу\s+(?:выполнить|помочь|предоставить)\b", re.IGNORECASE)),
    ("FORBIDDEN_OUTPUT", re.compile(r"\bответьте\s+только\b", re.IGNORECASE)),
)


def _fragment(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 40)
    end = min(len(text), match.end() + 40)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def check_materials(texts: Iterable[str]) -> list[GuardrailFinding]:
    """Ищет в материалах формулировки готового судебного акта."""
    findings: list[GuardrailFinding] = []
    for text in texts:
        for code, pattern in _MATERIAL_PATTERNS:
            for match in pattern.finditer(text or ""):
                findings.append(
                    GuardrailFinding(
                        code=code,
                        message="в материалах есть формулировка судебного акта — возможна утечка исхода",
                        fragment=_fragment(text, match),
                    )
                )
    return findings


def check_output(text: str) -> list[GuardrailFinding]:
    """Ищет в сгенерированном тексте служебные/модельные формулировки."""
    findings: list[GuardrailFinding] = []
    for code, pattern in _OUTPUT_PATTERNS:
        for match in pattern.finditer(text or ""):
            findings.append(
                GuardrailFinding(
                    code=code,
                    message="в проект попала служебная формулировка модели",
                    fragment=_fragment(text, match),
                )
            )
    return findings
