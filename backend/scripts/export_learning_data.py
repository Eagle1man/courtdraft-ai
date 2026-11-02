"""Выгрузить материалы теста для разбора: дела, тексты, проекты, отзывы.

Один каталог на студента, внутри — оба его дела. В `manifest.jsonl`
одна строка на дело: код участника, модель, оценка, вердикт. Это позволяет
сравнить сильную и слабую модель на одинаковых вопросах, не выгружая
базу вручную.

Материалы дел — персональные данные. По умолчанию выгружаются только
синтетические дела; для тестовых обезличенных материалов нужен явный
флаг `--allow-personal`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.repositories import (
    AuditRepository,
    CaseRepository,
    DocumentRepository,
    DraftRepository,
    FeedbackRepository,
)
from app.services.case_service import db, session_scope


def _student_code(entries: list[Any]) -> str:
    for entry in entries:
        if entry.action == "case.created":
            return str((entry.details or {}).get("student_code") or "")
    return ""


def _model(entries: list[Any]) -> str:
    for entry in entries:
        if entry.action == "model.assigned.strong":
            return str((entry.details or {}).get("model") or "")
    for entry in entries:
        if entry.action == "model.assigned.weak":
            return str((entry.details or {}).get("model") or "")
    for entry in entries:
        if entry.action == "draft.generated":
            return str((entry.details or {}).get("model") or "")
    return ""


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


async def export(out_dir: Path, *, allow_personal: bool) -> int:
    cfg = get_settings()
    if not await db.connect(cfg):
        print("БД недоступна")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    skipped: list[str] = []

    try:
        async with session_scope(db.factory) as session:
            cases = await CaseRepository(session).list(limit=2000)
            documents = DocumentRepository(session)
            drafts = DraftRepository(session)
            feedback = FeedbackRepository(session)
            audit = AuditRepository(session)

            for case in sorted(cases, key=lambda item: item.case_number):
                entries = await audit.list_for_case(case.id, limit=500)
                # list_for_case отдаёт новые сверху; для порядка дел
                # студента нужен самый ранний case.created.
                created = [e for e in reversed(entries) if e.action == "case.created"]
                student = _student_code(created) or "anon"
                model = _model(entries)

                feedback_rows = await feedback.list(case_id=case.id, limit=1000)
                ratings = [row.rating for row in feedback_rows]
                verdicts = [row.verdict for row in feedback_rows]

                row: dict[str, Any] = {
                    "student_code": student,
                    "case_id": str(case.id),
                    "case_number": case.case_number,
                    "case_type": case.case_type,
                    "court_name": case.court_name,
                    "is_synthetic": case.is_synthetic,
                    "model": model,
                    "feedback_count": len(feedback_rows),
                    "average_rating": (
                        round(sum(ratings) / len(ratings), 2) if ratings else 0.0
                    ),
                    "verdicts": verdicts,
                    "created_at": (
                        case.created_at.isoformat() if case.created_at else ""
                    ),
                }

                if not case.is_synthetic and not allow_personal:
                    skipped.append(case.case_number)
                    row["skipped"] = "не синтетическое дело; нужен --allow-personal"
                    manifest.append(row)
                    continue

                case_dir = out_dir / "students" / student / (
                    f"{case.case_number}_{str(case.id)[:8]}"
                )
                case_dir.mkdir(parents=True, exist_ok=True)

                docs = await documents.list_for_case(case.id)
                _write_jsonl(
                    case_dir / "documents.jsonl",
                    [
                        {
                            "filename": doc.filename,
                            "document_type": doc.document_type,
                            "mime_type": doc.mime_type,
                            "page_count": doc.page_count,
                            "sha256": doc.sha256,
                            "text": doc.text,
                            "metadata": doc.metadata_json,
                        }
                        for doc in docs
                    ],
                )

                draft = await drafts.get_for_case(case.id)
                versions: list[Any] = []
                if draft is not None:
                    versions = await drafts.list_versions(draft.id)
                    if versions:
                        (case_dir / "draft.txt").write_text(
                            versions[-1].text, encoding="utf-8"
                        )

                _write_jsonl(
                    case_dir / "feedback.jsonl",
                    [
                        {
                            "rating": item.rating,
                            "verdict": item.verdict,
                            "model": item.model_name,
                            "comment": item.comment,
                            "author_role": item.author_role,
                            "author_name": item.author_name,
                            "anonymized": item.is_anonymized,
                            "draft_version": item.draft_version,
                        }
                        for item in feedback_rows
                    ],
                )

                row["documents"] = len(docs)
                row["draft_chars"] = (
                    len(versions[-1].text) if draft is not None and versions else 0
                )
                manifest.append(row)

        _write_jsonl(out_dir / "manifest.jsonl", manifest)
    finally:
        await db.disconnect()

    print(f"выгружено дел: {len(manifest)} -> {out_dir}")
    if skipped:
        print(
            "пропущены несинтетические дела (нужен --allow-personal): "
            + ", ".join(skipped)
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/learning"),
        help="каталог выгрузки",
    )
    parser.add_argument(
        "--allow-personal",
        action="store_true",
        help="выгружать и несинтетические дела (только для теста с согласия)",
    )
    args = parser.parse_args()
    return asyncio.run(export(args.out, allow_personal=args.allow_personal))


if __name__ == "__main__":
    sys.exit(main())
