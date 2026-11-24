"""Оркестратор паспорт-ИИ.

Этапы сохраняются в каталоге дела и могут возобновляться. Подтверждение не
пишет паспорт напрямую: сервис возвращает проверенный проект, а роутер сохраняет
его через существующий `CasePassportRepository` с бэкапом прежней версии.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

from . import assembler, classify, extract, reader, reconcile
from .models import (
    DocClass,
    DocIntake,
    FactStatus,
    FieldDraft,
    Fragment,
    Stage,
    now_iso,
)


class PassportService:
    def __init__(
        self,
        storage_root: Path,
        llm: Any,
        contract: dict[str, Any],
        field_map: dict[str, Any],
        ocr_hook: Callable[[bytes, int], str] | None = None,
    ) -> None:
        self.root = Path(storage_root)
        self.llm = llm
        self.contract = contract
        self.field_map = field_map
        self.ocr_hook = ocr_hook  # не используется: OCR выполняется существующим контуром

    def case_dir(self, case_id: str) -> Path:
        directory = self.root / str(case_id) / "passport_ai"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _save(self, case_id: str, name: str, data: Any) -> None:
        (self.case_dir(case_id) / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=1, default=str),
            "utf-8",
        )

    def _load(self, case_id: str, name: str, default: Any) -> Any:
        path = self.case_dir(case_id) / name
        return json.loads(path.read_text("utf-8")) if path.exists() else default

    def roles(self) -> dict[str, dict[str, Any]]:
        return cast(dict[str, dict[str, Any]], self.field_map.get("fields", {}))

    def role_descriptions(self) -> dict[str, str]:
        return {
            role: str(spec.get("hint", role))
            for role, spec in self.roles().items()
        }

    async def run(
        self,
        case_id: str,
        files: Iterable[tuple[str, bytes]] = (),
        existing_docs: Iterable[DocIntake] = (),
        *,
        fragments_by_doc: dict[str, list[Fragment]] | None = None,
        force_reprocess_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Анализ документов.

        В рабочем приложении документы передаются из БД (`existing_docs` +
        `fragments_by_doc`). `files` оставлен для синтетических TXT-тестов;
        DOCX/PDF/OCR повторно не читаются.
        """
        documents = list(existing_docs)
        fragments: dict[str, list[Fragment]] = {}
        seen_sha = {document.sha256: document.document_id for document in documents}

        for filename, data in files:
            document = reader.intake_one(uuid.uuid4().hex, filename, data)
            duplicate_of = seen_sha.get(document.sha256)
            if duplicate_of:
                document.duplicate_of = duplicate_of
            else:
                seen_sha[document.sha256] = document.document_id
            text = data.decode("utf-8", "replace")
            document.read_ok = bool(text.strip())
            documents.append(document)
            fragments[document.document_id] = reader.fragments_from_text(
                document.document_id, filename, text
            )

        if fragments_by_doc:
            fragments.update(fragments_by_doc)
        self._save(case_id, "intake.json", [item.model_dump() for item in documents])

        force = force_reprocess_ids or set()
        for document in documents:
            if document.duplicate_of:
                continue
            if document.document_id in fragments:
                continue
            cached = self._load(
                case_id, f"frag_{document.document_id}.json", None
            )
            if cached is not None and document.read_ok and document.document_id not in force:
                from .models import Fragment as FragmentModel

                fragments[document.document_id] = [
                    FragmentModel(**item) for item in cached
                ]
                continue
            fragments[document.document_id] = []
            if document.read_ok is False:
                document.errors.append("нет извлечённого текста")
        self._save(case_id, "intake.json", [item.model_dump() for item in documents])

        classes: dict[str, DocClass] = {}
        for document in documents:
            if document.duplicate_of or document.read_ok is False:
                continue
            doc_fragments = fragments.get(document.document_id, [])
            classes[document.document_id] = await classify.classify_doc(
                self.llm, document, doc_fragments
            )
        self._save(
            case_id,
            "classes.json",
            {key: value.model_dump() for key, value in classes.items()},
        )

        all_facts = []
        kinds = {
            role: str(spec.get("kind", "text"))
            for role, spec in self.roles().items()
        }
        for document in documents:
            doc_fragments = fragments.get(document.document_id, [])
            if document.duplicate_of or not doc_fragments:
                continue
            text = "\n\n".join(fragment.text for fragment in doc_fragments)
            llm_facts = await extract.extract_facts_llm(
                self.llm,
                document,
                text,
                doc_fragments,
                self.role_descriptions(),
                kinds,
            )
            deterministic_facts = extract.extract_facts_det(
                document,
                doc_fragments,
                self.role_descriptions(),
                kinds,
            )
            doc_class = classes.get(document.document_id)
            for fact in llm_facts + deterministic_facts:
                fact.doc_type = doc_class.doc_type if doc_class else ""
            all_facts.extend(llm_facts + deterministic_facts)
        self._save(
            case_id, "facts.json", [fact.model_dump() for fact in all_facts]
        )

        chosen, conflicts, findings = reconcile.reconcile(all_facts, self.roles())
        findings += reconcile.arithmetic_checks(all_facts)
        self._save(
            case_id, "conflicts.json", [item.model_dump() for item in conflicts]
        )

        draft, fields = assembler.assemble(
            chosen,
            self.roles(),
            conflicts,
            field_map=self.field_map,
            documents=documents,
            classes=classes,
        )
        findings += assembler.validate_draft(
            draft, self.contract, self.field_map, fields
        )
        self._save(case_id, "draft_passport.json", draft)
        self._save(case_id, "fields.json", [item.model_dump() for item in fields])
        self._save(
            case_id, "validation.json", [item.model_dump() for item in findings]
        )
        self._save(
            case_id,
            "state.json",
            {
                "stage": Stage.VALIDATED,
                "updated_at": now_iso(),
                "documents": len(documents),
            },
        )
        return self.status(case_id)

    def status(self, case_id: str) -> dict[str, Any]:
        state = self._load(case_id, "state.json", {"stage": Stage.NEW})
        fields = self._load(case_id, "fields.json", [])
        conflicts = self._load(case_id, "conflicts.json", [])
        validation = self._load(case_id, "validation.json", [])
        blocking = [item for item in validation if item.get("level") == "error"]
        return {
            "stage": state.get("stage"),
            "updated_at": state.get("updated_at"),
            "fields_total": len(fields),
            "fields_filled": sum(
                1 for field in fields if field.get("value") not in (None, "")
            ),
            "conflicts": len(conflicts),
            "blocking_issues": len(blocking),
            "can_confirm": (
                not blocking
                and not any(item.get("blocking") for item in conflicts)
                and state.get("stage") in {Stage.VALIDATED, Stage.CONFIRMED}
            ),
        }

    def analysis(self, case_id: str) -> dict[str, Any]:
        return {
            "documents": self._load(case_id, "intake.json", []),
            "facts": self._load(case_id, "facts.json", []),
            "conflicts": self._load(case_id, "conflicts.json", []),
            "fields": self._load(case_id, "fields.json", []),
            "validation": self._load(case_id, "validation.json", []),
        }

    def update_field(
        self, case_id: str, role: str, value: Any, action: str
    ) -> dict[str, Any]:
        if action not in {"confirmed", "edited", "rejected"}:
            raise ValueError("action должен быть confirmed|edited|rejected")
        fields = [
            FieldDraft(**item) for item in self._load(case_id, "fields.json", [])
        ]
        target = next((field for field in fields if field.role == role), None)
        if target is None:
            raise ValueError("неизвестная роль")
        target.user_action = action
        if action == "edited":
            target.value = self._coerce_user_value(target.kind, value)
            target.status = FactStatus.SUPPORTED
        elif action == "rejected":
            target.value = None
            target.status = FactStatus.NOT_FOUND

        draft = self._load(case_id, "draft_passport.json", {})
        if action == "rejected":
            for pointer in assembler._pointers(target.pointer):
                self._delete_pointer(draft, pointer)
        else:
            for pointer in assembler._pointers(target.pointer):
                assembler._set_pointer(draft, pointer, target.value)
        self._sync_derived(draft, fields)

        validation = assembler.validate_draft(
            draft, self.contract, self.field_map, fields
        )
        self._save(case_id, "fields.json", [item.model_dump() for item in fields])
        self._save(case_id, "draft_passport.json", draft)
        self._save(
            case_id, "validation.json", [item.model_dump() for item in validation]
        )
        self._save(
            case_id,
            "state.json",
            {"stage": Stage.VALIDATED, "updated_at": now_iso()},
        )
        return {"ok": True}

    def confirm(self, case_id: str) -> dict[str, Any]:
        state = self.status(case_id)
        if not state["can_confirm"]:
            return {
                "ok": False,
                "error": "есть блокирующие замечания или неразрешённые конфликты",
            }
        draft = self._load(case_id, "draft_passport.json", {})
        fields = [
            FieldDraft(**item) for item in self._load(case_id, "fields.json", [])
        ]
        validation = assembler.validate_draft(
            draft, self.contract, self.field_map, fields
        )
        errors = [item for item in validation if item.level == "error"]
        if errors:
            return {
                "ok": False,
                "error": f"паспорт не прошёл проверку: {errors[0].message}",
            }
        return {"ok": True, "draft": draft}

    def mark_confirmed(self, case_id: str) -> None:
        self._save(
            case_id,
            "state.json",
            {"stage": Stage.CONFIRMED, "updated_at": now_iso()},
        )

    def _coerce_user_value(self, kind: str, value: Any) -> Any:
        if kind == "money_kopecks":
            if isinstance(value, str) and not re.fullmatch(r"\d+", value.strip()):
                parsed = extract.parse_money(value)
                if parsed is None:
                    raise ValueError("не удалось разобрать денежное значение")
                return int((parsed * 100).to_integral_value())
            return int(value)
        if kind == "int":
            return int(value)
        if kind == "date_ru":
            raw = str(value)
            iso = extract.parse_date(raw)
            return assembler.coerce("date_ru", iso) if iso else raw
        if kind == "list":
            return value if isinstance(value, list) else [str(value)]
        return str(value)

    def _sync_derived(
        self, draft: dict[str, Any], fields: list[FieldDraft]
    ) -> None:
        by_role = {field.role: field for field in fields}
        existing_claims = draft.get("claims")
        existing_by_kind = (
            {item.get("kind"): item for item in existing_claims}
            if isinstance(existing_claims, list)
            else {}
        )
        claims: list[dict[str, Any]] = []
        for spec in self.field_map.get("derived", {}).get("claims", []):
            field = by_role.get(spec.get("amount_role", ""))
            if field is None or field.value is None:
                continue
            kind = str(spec.get("kind", "other"))
            claim = dict(existing_by_kind.get(kind, {}))
            claim.update(
                {
                    "n": int(spec.get("n", len(claims) + 1)),
                    "kind": kind,
                    "amount_kopecks": field.value,
                    "text": str(spec.get("text", kind)),
                }
            )
            claims.append(claim)
        draft["claims"] = claims
        gaps = [
            str(self.roles().get(role, {}).get("hint", role))
            for role in self.field_map.get("gap_roles", [])
            if by_role.get(role) is None or by_role[role].value in (None, "")
        ]
        if gaps:
            draft["gaps"] = gaps
        elif "gaps" in draft:
            draft["gaps"] = []

    @staticmethod
    def _delete_pointer(draft: dict[str, Any], pointer: str) -> None:
        parts = [part for part in pointer.split("/") if part]
        if not parts:
            return
        current: Any = draft
        for part in parts[:-1]:
            if not isinstance(current, dict) or part not in current:
                return
            current = current[part]
        if isinstance(current, dict):
            current.pop(parts[-1], None)
