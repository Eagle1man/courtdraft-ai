"""Квота сильной модели: первые N дел, фиксация назначения, границы."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config import Settings
from app.model_routing.quota import STRONG_ACTION, WEAK_ACTION, ModelRouter


@dataclass
class _Entry:
    action: str
    case_id: uuid.UUID | None
    details: dict[str, Any] = field(default_factory=dict)


class _FakeAudit:
    def __init__(self) -> None:
        self.entries: list[_Entry] = []

    async def list_for_case(self, case_id: uuid.UUID, *, limit: int = 100) -> list[_Entry]:
        found = [entry for entry in self.entries if entry.case_id == case_id]
        return list(reversed(found))[:limit]

    async def count_by_action(self, action: str) -> int:
        return sum(1 for entry in self.entries if entry.action == action)

    async def log(
        self, action: str, *, case_id: uuid.UUID | None = None, **details: Any
    ) -> _Entry:
        entry = _Entry(action=action, case_id=case_id, details=dict(details))
        self.entries.append(entry)
        return entry


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "model_routing_enabled": True,
        "strong_model": "strong:7b",
        "weak_model": "weak:1.5b",
        "strong_model_quota": 2,
        "ollama_model": "weak:1.5b",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_first_cases_get_strong_then_weak() -> None:
    audit = _FakeAudit()
    router = ModelRouter(None, _settings(), audit=audit)  # type: ignore[arg-type]

    first = await router.assign(uuid.uuid4())
    second = await router.assign(uuid.uuid4())
    third = await router.assign(uuid.uuid4())

    assert (first.tier, first.slot) == ("strong", 1)
    assert (second.tier, second.slot) == ("strong", 2)
    assert third.tier == "weak"
    assert third.model == "weak:1.5b"
    assert [entry.action for entry in audit.entries].count(STRONG_ACTION) == 2
    assert [entry.action for entry in audit.entries].count(WEAK_ACTION) == 1


@pytest.mark.asyncio
async def test_assignment_is_stable_for_same_case() -> None:
    audit = _FakeAudit()
    router = ModelRouter(None, _settings(), audit=audit)  # type: ignore[arg-type]
    case_id = uuid.uuid4()

    first = await router.assign(case_id)
    again = await router.assign(case_id)

    assert first.model == again.model == "strong:7b"
    assert again.existing is True
    assert len(audit.entries) == 1


@pytest.mark.asyncio
async def test_disabled_routing_uses_default_model_without_logging() -> None:
    audit = _FakeAudit()
    settings = _settings(model_routing_enabled=False)
    router = ModelRouter(None, settings, audit=audit)  # type: ignore[arg-type]

    assignment = await router.assign(uuid.uuid4())

    assert assignment.model == "weak:1.5b"
    assert assignment.tier == "default"
    assert audit.entries == []


@pytest.mark.asyncio
async def test_zero_quota_gives_everyone_weak() -> None:
    audit = _FakeAudit()
    router = ModelRouter(None, _settings(strong_model_quota=0), audit=audit)  # type: ignore[arg-type]

    assignment = await router.assign(uuid.uuid4())

    assert assignment.tier == "weak"
    assert assignment.model == "weak:1.5b"


@pytest.mark.asyncio
async def test_strong_equal_weak_does_not_burn_slots() -> None:
    audit = _FakeAudit()
    settings = _settings(strong_model="same:model", weak_model="same:model")
    router = ModelRouter(None, settings, audit=audit)  # type: ignore[arg-type]

    assignment = await router.assign(uuid.uuid4())

    assert assignment.tier == "weak"
    assert audit.entries[0].action == WEAK_ACTION
