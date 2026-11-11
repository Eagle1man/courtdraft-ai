"""Лёгкий GraphRAG для материалов дела.

Граф: дело → документы → факты → суммы/нормы/стороны. Без Neo4j:
узлы и рёбра живут в памяти на один проект. Задача — не «магия», а
детерминированный отбор связанных фактов и норм для промпта.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.agents.evidence_pack import EvidencePack


@dataclass
class KnowledgeGraph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, node_id: str, kind: str, text: str = "", **meta: Any) -> str:
        self.nodes[node_id] = {"kind": kind, "text": text, **meta}
        return node_id

    def link(self, src: str, dst: str, kind: str) -> None:
        if src in self.nodes and dst in self.nodes:
            self.edges.append((src, dst, kind))

    @classmethod
    def from_pack(cls, pack: EvidencePack) -> KnowledgeGraph:
        graph = cls()
        case_id = "case"
        graph.add(case_id, "case", pack.case_number, court=pack.court_name)
        for name in pack.documents:
            doc_id = f"doc:{name}"
            graph.add(doc_id, "document", name)
            graph.link(case_id, doc_id, "has_document")
        for index, item in enumerate(pack.items):
            item_id = f"item:{index}"
            graph.add(
                item_id,
                "fact",
                item.text,
                category=item.category,
                source=item.source_document,
            )
            doc_id = f"doc:{item.source_document}"
            if doc_id in graph.nodes:
                graph.link(doc_id, item_id, "contains")
            else:
                graph.link(case_id, item_id, "contains")
            for amount in item.amounts:
                amount_id = f"amount:{amount}"
                graph.add(amount_id, "amount", amount)
                graph.link(item_id, amount_id, "mentions_amount")
            for article, code in _laws(item.text):
                norm_id = f"norm:{article} {code}"
                graph.add(norm_id, "norm", f"ст. {article} {code}")
                graph.link(item_id, norm_id, "mentions_norm")
        for amount in pack.amounts:
            graph.add(f"amount:{amount}", "amount", amount)
            graph.link(case_id, f"amount:{amount}", "has_amount")
        for participant in pack.participants:
            party_id = f"party:{participant}"
            graph.add(party_id, "party", participant)
            graph.link(case_id, party_id, "has_party")
        return graph

    def context_for(self, section_key: str, *, limit: int = 8) -> str:
        """Связанные факты/суммы/нормы для промпта раздела.

        Не «первые восемь узлов», а выбор по категориям, которые нужны
        разделу. Иначе в промпт попадали случайные факты и GraphRAG
        только увеличивал контекст.
        """
        priorities = {
            "established": ("claims", "evidence", "established_facts", "protocol_findings"),
            "motivating": ("claims", "objections", "evidence", "legal_position"),
            "operative": ("claims", "decision_items", "evidence"),
            "descriptive": ("claims", "objections", "evidence"),
        }.get(section_key, ("claims", "evidence", "legal_position"))

        def rank(node: dict[str, Any]) -> tuple[int, int, int]:
            kind = str(node.get("kind"))
            category = str(node.get("category") or "")
            try:
                category_rank = priorities.index(category)
            except ValueError:
                category_rank = len(priorities)
            kind_rank = {"fact": 0, "norm": 1, "amount": 2}.get(kind, 3)
            return (category_rank, kind_rank, -len(str(node.get("text") or "")))

        selected: list[dict[str, Any]] = []
        for node in self.nodes.values():
            kind = str(node.get("kind"))
            if kind not in {"fact", "norm", "amount"}:
                continue
            if kind == "fact" and str(node.get("category") or "") not in priorities:
                continue
            selected.append(node)
        selected.sort(key=rank)

        lines: list[str] = []
        seen: set[str] = set()
        for node in selected:
            text = str(node.get("text") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            lines.append(f"[{node['kind']}] {text[:500]}")
            if len(lines) >= limit:
                break
        return "\n".join(lines)

    def summary(self) -> dict[str, int]:
        kinds: dict[str, int] = {}
        for node in self.nodes.values():
            kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
        return {**kinds, "edges": len(self.edges)}


_LAW_RE = re.compile(
    r"(?:ст\.|стать[яейию]+)\s*(\d+(?:\.\d+)*)\s*([А-ЯЁ][А-ЯЁ ]{0,10})",
    re.IGNORECASE,
)


def _laws(text: str) -> set[tuple[str, str]]:
    return {
        (match.group(1), " ".join(match.group(2).upper().split()))
        for match in _LAW_RE.finditer(text or "")
    }
