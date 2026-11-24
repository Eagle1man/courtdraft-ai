"""Адаптер к существующему валидатору паспорта.

Не дублирует правила: вызывает `CasePassport.model_validate` и
`validate_invariants()` из `app.ingestion.passport`.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.ingestion.passport import CasePassport


def validate_existing(draft: dict[str, Any]) -> list[dict[str, str]]:
    try:
        passport = CasePassport.model_validate(draft)
    except ValidationError as exc:
        output: list[dict[str, str]] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", []))
            output.append(
                {
                    "level": "error",
                    "code": "SCHEMA",
                    "field": location,
                    "message": str(error.get("msg", "ошибка схемы")),
                }
            )
        return output
    return [
        {
            "level": "error" if issue.severity == "P0" else "warning",
            "code": issue.code,
            "field": "",
            "message": issue.reason,
        }
        for issue in passport.validate_invariants()
    ]
