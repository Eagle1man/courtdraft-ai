"""Датасет проверок резолютивной части: загрузка и разбор."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PATH = Path(__file__).parent / "data" / "resol_fixtures.json"

META_DEFAULTS: dict[str, Any] = {
    "claim_date": None,
    "claim_price": None,
    "paid_fee": None,
    "claimant_exempt": False,
    "consumer_case": False,
    "default_judgment": False,
}


@dataclass(frozen=True)
class Fixture:
    id: str
    kind: str
    text: str
    expected: frozenset[str]
    allow: frozenset[str] = frozenset()
    meta: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""


def load() -> tuple[dict[str, Any], list[Fixture]]:
    data: dict[str, Any] = json.loads(PATH.read_text(encoding="utf-8"))
    fixtures = [
        Fixture(
            id=f["id"],
            kind=f["kind"],
            text=f["text"],
            expected=frozenset(e["id"] for e in f.get("expected", [])),
            allow=frozenset(f.get("allow", [])),
            meta={**META_DEFAULTS, **f.get("meta", {})},
            note=f.get("note", ""),
        )
        for f in data["fixtures"]
    ]
    return data, fixtures
