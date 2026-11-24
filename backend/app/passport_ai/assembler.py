"""Сборка проекта паспорта по обнаруженному контракту.

Модуль не знает имён полей паспорта в коде: указатели, типы и правила сборки
массивов приходят из `field_map.json`. Валидатор проекта вызывается через
адаптер к существующему `CasePassport`.
"""

from __future__ import annotations

import importlib
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import (
    Conflict,
    DocClass,
    DocIntake,
    FactCandidate,
    FactStatus,
    FieldDraft,
    Locator,
    ValidationFinding,
)


def _set_pointer(draft: dict[str, Any], pointer: str, value: Any) -> None:
    parts = [part for part in pointer.split("/") if part]
    if not parts:
        return
    current: Any = draft
    for part in parts[:-1]:
        if isinstance(current, list):
            if not part.isdigit():
                return
            index = int(part)
            while len(current) <= index:
                current.append({})
            if not isinstance(current[index], (dict, list)):
                current[index] = {}
            current = current[index]
        else:
            next_value = current.get(part)
            if not isinstance(next_value, (dict, list)):
                next_value = {}
                current[part] = next_value
            current = next_value
    last = parts[-1]
    if isinstance(current, list) and last.isdigit():
        index = int(last)
        while len(current) <= index:
            current.append(None)
        current[index] = value
    elif isinstance(current, dict):
        current[last] = value


def _pointers(pointer: str | list[str]) -> list[str]:
    return pointer if isinstance(pointer, list) else [pointer]


def coerce(kind: str, value: Any) -> Any:
    if isinstance(value, Decimal):
        if kind == "money_kopecks":
            return int((value * 100).to_integral_value())
        return f"{value:.2f}"
    if kind == "money_kopecks" and isinstance(value, (int, float, str)):
        return int((Decimal(str(value)) * 100).to_integral_value())
    if kind in {"date_ru", "date"} and isinstance(value, str):
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except ValueError:
            return value
    if kind == "int":
        return int(value)
    return value


def _display_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return str(value)


def _derived_fact(
    role: str,
    value: Any,
    sources: list[FactCandidate],
    note: str,
) -> FactCandidate:
    locator = sources[0].locator if sources else Locator(
        document_id="derived", filename="программный расчёт"
    )
    return FactCandidate(
        role=role,
        value_raw=_display_value(value),
        value_norm=value,
        quote=f"DERIVED: {note}",
        locator=locator,
        status=FactStatus.DERIVED,
        confidence=1.0,
        model="rules",
        note=note,
        source_fact_ids=[source.id for source in sources],
    )


def derive_missing(
    chosen: dict[str, FactCandidate | None],
    roles: dict[str, dict[str, Any]],
    field_map: dict[str, Any],
) -> dict[str, FactCandidate]:
    derived: dict[str, FactCandidate] = {}
    for role, spec in field_map.get("derived_fields", {}).items():
        if chosen.get(role) is not None:
            continue
        operation = spec.get("op")
        sources: list[FactCandidate] = []
        value: Any = None
        note = ""
        if operation == "copy":
            source = chosen.get(spec.get("source", ""))
            if source is not None:
                value = source.value_norm
                sources = [source]
                note = f"копия {spec.get('source')}"
        elif operation in {"add", "subtract"}:
            left = chosen.get(spec.get("left", ""))
            right = chosen.get(spec.get("right", ""))
            if (
                left is not None
                and right is not None
                and isinstance(left.value_norm, Decimal)
                and isinstance(right.value_norm, Decimal)
            ):
                value = (
                    left.value_norm + right.value_norm
                    if operation == "add"
                    else left.value_norm - right.value_norm
                )
                sources = [left, right]
                note = f"{spec.get('left')} {operation} {spec.get('right')}"
        elif operation == "inclusive_days":
            start_fact = chosen.get(spec.get("start", ""))
            end_fact = chosen.get(spec.get("end", ""))
            if start_fact is not None and end_fact is not None:
                try:
                    start = date.fromisoformat(str(start_fact.value_norm))
                    end = date.fromisoformat(str(end_fact.value_norm))
                    value = (end - start).days + 1
                    sources = [start_fact, end_fact]
                    note = "обе границы периода включены"
                except ValueError:
                    value = None
        if value is not None:
            derived[role] = _derived_fact(role, value, sources, note)
    return derived


def _build_claims(
    draft: dict[str, Any],
    chosen: dict[str, FactCandidate | None],
    field_map: dict[str, Any],
) -> None:
    claims: list[dict[str, Any]] = []
    for spec in field_map.get("derived", {}).get("claims", []):
        fact = chosen.get(spec.get("amount_role", ""))
        if fact is None or fact.value_norm is None:
            continue
        amount = coerce("money_kopecks", fact.value_norm)
        item: dict[str, Any] = {
            "n": int(spec.get("n", len(claims) + 1)),
            "kind": str(spec.get("kind", "other")),
            "amount_kopecks": amount,
            "text": str(spec.get("text", spec.get("kind", "требование"))),
        }
        period_roles = spec.get("period_roles") or []
        if len(period_roles) == 2:
            start = chosen.get(period_roles[0])
            end = chosen.get(period_roles[1])
            if start is not None and end is not None:
                item["period"] = [
                    coerce("date_ru", start.value_norm),
                    coerce("date_ru", end.value_norm),
                ]
        claims.append(item)
    if claims:
        draft["claims"] = claims


def _build_events(
    draft: dict[str, Any],
    chosen: dict[str, FactCandidate | None],
    field_map: dict[str, Any],
) -> None:
    events: list[dict[str, Any]] = []
    for spec in field_map.get("derived", {}).get("events", []):
        fact = chosen.get(spec.get("date_role", ""))
        if fact is None or fact.value_norm is None:
            continue
        values = {
            role: _display_value(item.value_norm)
            for role, item in chosen.items()
            if item is not None and item.value_norm is not None
        }
        template = str(spec.get("fact", ""))
        try:
            description = template.format(**values)
        except (KeyError, ValueError):
            description = template
        events.append(
            {
                "date": coerce("date_ru", fact.value_norm),
                "code": str(spec.get("code", "EVENT")),
                "fact": description,
            }
        )
    if events:
        draft["events"] = events


def _build_documents(
    draft: dict[str, Any],
    documents: list[DocIntake],
    classes: dict[str, DocClass],
    field_map: dict[str, Any],
) -> None:
    rules = field_map.get("derived", {}).get("documents", {})
    appendix_patterns = [str(item).lower() for item in rules.get("appendix_patterns", [])]
    exclude_patterns = [str(item).lower() for item in rules.get("exclude_patterns", [])]
    title_by_type = rules.get("title_by_type", {})
    items: list[dict[str, Any]] = []
    for document in documents:
        if document.duplicate_of or document.read_ok is False:
            continue
        name = document.filename.lower()
        doc_class = classes.get(document.document_id)
        doc_type = doc_class.doc_type if doc_class else "other"
        title = str(title_by_type.get(doc_type, Path(document.filename).stem))
        appendix = any(pattern in name for pattern in appendix_patterns)
        if any(pattern in name for pattern in exclude_patterns):
            appendix = False
        items.append(
            {
                "file": document.filename,
                "title": title,
                "title_genitive": title,
                "sheets": int(document.pages or 1),
                "appendix": appendix,
            }
        )
    if items:
        draft["documents"] = items


def assemble(
    chosen: dict[str, FactCandidate | None],
    roles: dict[str, dict[str, Any]],
    conflicts: list[Conflict],
    *,
    field_map: dict[str, Any] | None = None,
    documents: list[DocIntake] | None = None,
    classes: dict[str, DocClass] | None = None,
) -> tuple[dict[str, Any], list[FieldDraft]]:
    mapping = field_map or {"fields": roles}
    derived = derive_missing(chosen, roles, mapping)
    effective = dict(chosen)
    effective.update(derived)

    draft: dict[str, Any] = {}
    for pointer, value in mapping.get("defaults", {}).items():
        _set_pointer(draft, pointer, value)

    fields: list[FieldDraft] = []
    conflict_by_role = {conflict.role: conflict.id for conflict in conflicts}
    for role, spec in roles.items():
        fact = effective.get(role)
        pointer = spec.get("pointer", "")
        field = FieldDraft(
            role=role,
            pointer=pointer,
            kind=str(spec.get("kind", "text")),
            required=bool(spec.get("required")),
            conflict_id=conflict_by_role.get(role),
        )
        if fact is not None and fact.value_norm is not None:
            field.value = coerce(field.kind, fact.value_norm)
            field.status = fact.status
            field.fact_ids = fact.source_fact_ids or [fact.id]
            for target in _pointers(pointer):
                _set_pointer(draft, target, field.value)
        else:
            field.status = (
                FactStatus.CONFLICT if field.conflict_id else FactStatus.NOT_FOUND
            )
        fields.append(field)

    _build_claims(draft, effective, mapping)
    _build_events(draft, effective, mapping)
    _build_documents(draft, documents or [], classes or {}, mapping)

    missing_gaps = [
        str(roles.get(role, {}).get("hint", role))
        for role in mapping.get("gap_roles", [])
        if effective.get(role) is None
    ]
    if missing_gaps:
        draft["gaps"] = missing_gaps
    return draft, fields


def validate_draft(
    draft: dict[str, Any],
    contract: dict[str, Any],
    field_map: dict[str, Any],
    fields: list[FieldDraft],
) -> list[ValidationFinding]:
    """Внешний валидатор проекта + внутренние проверки типов/обязательности."""
    findings: list[ValidationFinding] = []
    validator_path = str(field_map.get("validator") or "").strip()
    if validator_path and ":" in validator_path:
        module_name, function_name = validator_path.split(":", 1)
        try:
            validator = getattr(importlib.import_module(module_name), function_name)
            for item in validator(draft) or []:
                if isinstance(item, dict):
                    findings.append(
                        ValidationFinding(
                            level=str(item.get("level", "error")),
                            code=str(item.get("code", "EXTERNAL")),
                            message=str(item.get("message", "")),
                            field=str(item.get("field", "")),
                        )
                    )
                else:
                    findings.append(
                        ValidationFinding(
                            level="error", code="EXTERNAL", message=str(item)
                        )
                    )
        except Exception as exc:
            findings.append(
                ValidationFinding(
                    level="error",
                    code="VALIDATOR_ERROR",
                    message=f"внешний валидатор не вызван: {exc}",
                )
            )

    for field in fields:
        name = field.role
        if field.required and field.value in (None, ""):
            findings.append(
                ValidationFinding(
                    level="error",
                    code="REQUIRED_EMPTY",
                    field=name,
                    message="обязательное поле не заполнено",
                )
            )
        if field.value is None:
            continue
        if field.kind == "money_kopecks":
            valid = isinstance(field.value, int) and field.value >= 0
        elif field.kind == "int":
            valid = isinstance(field.value, int)
        elif field.kind == "date_ru":
            valid = isinstance(field.value, str) and bool(
                re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", field.value)
            )
            if valid:
                try:
                    datetime.strptime(field.value, "%d.%m.%Y")
                except ValueError:
                    valid = False
        elif field.kind == "list":
            valid = isinstance(field.value, list)
        else:
            valid = isinstance(field.value, str)
        if not valid:
            findings.append(
                ValidationFinding(
                    level="warning",
                    code="TYPE_MISMATCH",
                    field=name,
                    message=f"некорректный тип/формат для {field.kind}",
                )
            )
    return findings
