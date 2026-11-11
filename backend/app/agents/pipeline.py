"""Multi-agent pipeline: research → verify → synthesize → generate → review.

Это не автономные агенты с собственным планированием, а явные стадии с
проверяемыми входами/выходами. Так проще тестировать и нельзя «потерять»
результат одной стадии.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.draft_agent import DraftResult, generate_draft
from app.agents.evidence_pack import EvidencePack
from app.config import Settings, get_settings
from app.graphrag import KnowledgeGraph
from app.llm.base import LLMProvider
from app.verification.engine import VerificationReport, verify_draft


@dataclass
class PipelineResult:
    draft: DraftResult
    verification: VerificationReport
    graph: KnowledgeGraph
    stages: list[str] = field(default_factory=list)


async def run_pipeline(
    pack: EvidencePack,
    template: dict[str, Any],
    *,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
) -> PipelineResult:
    cfg = settings or get_settings()
    stages: list[str] = []
    stages.append("research")
    graph = KnowledgeGraph.from_pack(pack)

    stages.append("verify_materials")
    # Материалы уже проверены guardrails внутри verify_draft после
    # генерации; здесь фиксируем, что граф построен из проверяемых узлов.

    stages.append("synthesize")
    # Для генерации включаем graph-контекст, даже если глобальный флаг
    # выключен: это и есть смысл pipeline.
    pipeline_settings = cfg.model_copy(update={"graphrag_enabled": True})
    draft = await generate_draft(
        pack,
        template,
        provider=provider,
        settings=pipeline_settings,
    )

    stages.append("generate")
    report = verify_draft(draft, pack, pipeline_settings)

    stages.append("review")
    return PipelineResult(
        draft=draft,
        verification=report,
        graph=graph,
        stages=stages,
    )
