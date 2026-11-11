"""P0-проверки: единое ядро, даты, подстатьи, дубли, fallback."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.agents.draft_agent import _build_motivating_section
from app.agents.evidence_pack import EvidenceItem, EvidencePack, extract_dates
from app.config import Settings
from app.verification.engine import IssueType, Severity, verify_draft

ROOT = Path(__file__).resolve().parents[2]
CD_CORE = ROOT / "scripts" / "cd_core.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("cd_core_test", CD_CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["cd_core_test"] = module
    spec.loader.exec_module(module)
    return module


def _pack() -> EvidencePack:
    return EvidencePack(case_number="T-1")


def test_dates_dot_format_reaches_pack() -> None:
    assert extract_dates("Решение от 21.01.2013 года") == ["21 января 2013"]


def test_subarticles_in_laws() -> None:
    from app.verification.engine import _laws_in

    assert ("333.19", "НК") in _laws_in("ст. 333.19 НК РФ")


def test_model_sections_config() -> None:
    cfg = Settings(model_sections="established")
    assert cfg.allowed_model_sections == {"established"}


def test_motivating_fallback_not_empty() -> None:
    pack = _pack()
    pack.items.append(
        EvidenceItem(
            category="claims",
            text="Просит взыскать долг 1000 руб.",
            source_document="01_claim.txt",
            source_type="CLAIM",
            order=1,
        )
    )
    section = {"key": "motivating", "name": "Мотивировочная часть"}
    draft = _build_motivating_section(section, pack)
    assert draft.text.strip()
    assert draft.built_by == "rules"
    assert draft.needs_review is True


def test_duplicate_awards_flagged() -> None:
    from app.agents.draft_agent import DraftResult, SectionDraft

    pack = _pack()
    pack.items.append(
        EvidenceItem(
            category="claims",
            text="Просит взыскать долг 1000 руб.",
            source_document="01_claim.txt",
            source_type="CLAIM",
            order=1,
        )
    )
    draft = DraftResult(
        case_number="T-1",
        sections=[
            SectionDraft(
                key="operative",
                name="РЕШИЛ",
                text=(
                    "1. Взыскать с ответчика 1 000,00 руб.\n"
                    "2. Взыскать с ответчика 1 000,00 руб."
                ),
                is_mock=False,
                built_by="rules",
            )
        ],
    )
    report = verify_draft(draft, pack)
    duplicates = [
        issue
        for issue in report.issues
        if issue.type is IssueType.RESOL_TOTAL_MISMATCH
        and issue.severity is Severity.CRITICAL
        and "дважды" in issue.reason
    ]
    assert duplicates


def test_cd_core_parsers_and_profiles() -> None:
    core = _load_core()
    assert core.amounts_kop("1 561,37 руб. и 400 руб.") == [156137, 40000]
    assert "333.19 НК" in core.citations("ст. 333.19 НК РФ")
    assert core.dates_any("21.01.2013")[0].isoformat() == "2013-01-21"
    assert core.profile_for("ollama", "qwen2.5:1.5b").model_sections == ("established",)
    assert "motivating" in core.profile_for("openai", "deepseek-flash").model_sections

    ledger = core.Ledger(budget_rub=1.0)
    assert ledger.can_spend(0.5)
    ledger.add(0.25, case="x")
    assert ledger.remaining_rub == 0.75
    assert ledger.summary()["calls"] == 1
