"""Повторная проверка проекта из сохранённой версии (Этап 8).

Правило этапа: **исправление не повышает статус само по себе**. Статус
появляется только из проверки. Поэтому повторная проверка:

1. берёт текст последней версии — тот, что видит судья на экране;
2. собирает из него разделы (разбивка сохранена в `citations`);
3. считает отчёт заново, с нуля, и сохраняет его с номером версии.

Старый отчёт при этом остаётся привязанным к своей версии и помечается
устаревшим (`is_stale`), а не переписывается: история проверок — часть
аудита, и «переписать отчёт прошлой версии» значит потерять, что именно
проверяли.

Почему из `citations`, а не из полного текста: разбивка на разделы уже
сохранена вместе с версией, а `DraftResult.text` собирается по шаблону.
Если проверять пересобранный текст, можно проверить не то, что показано.
"""

from __future__ import annotations

from typing import Any

from app.agents.draft_agent import DraftResult, SectionDraft
from app.agents.evidence_pack import EvidencePack
from app.config import Settings
from app.models.entities import DraftVersion
from app.verification.engine import VerificationReport, verify_draft


def draft_from_version(version: DraftVersion) -> DraftResult:
    """Собрать проект из сохранённой версии для повторной проверки."""
    sections: list[SectionDraft] = []
    citations = version.citations if isinstance(version.citations, list) else []
    for raw in citations:
        if not isinstance(raw, dict):
            continue
        sections.append(_section_from(raw))
    if not sections:
        # Версия без разбивки (старые записи): проверяем весь текст одним
        # разделом — лучше проверить целиком, чем не проверить вовсе.
        sections = [
            SectionDraft(
                key="draft",
                name="Проект",
                text=version.text,
                is_mock=False,
                built_by="unknown",
            )
        ]
    return DraftResult(case_number="", sections=sections)


def _section_from(raw: dict[str, Any]) -> SectionDraft:
    return SectionDraft(
        key=str(raw.get("key", "")),
        name=str(raw.get("name", "")),
        text=str(raw.get("text", "")),
        is_mock=bool(raw.get("is_mock", False)),
        citations=[str(c) for c in raw.get("citations", []) or []],
        needs_review=bool(raw.get("needs_review", False)),
        latency_ms=int(raw.get("latency_ms", 0)),
        error=str(raw.get("error", "")),
        built_by=str(raw.get("built_by", "model")),
        gaps=[str(g) for g in raw.get("gaps", []) or []],
    )


def recheck_version(
    version: DraftVersion,
    pack: EvidencePack,
    settings: Settings | None = None,
) -> VerificationReport:
    """Посчитать отчёт заново по сохранённой версии."""
    return verify_draft(draft_from_version(version), pack, settings)
