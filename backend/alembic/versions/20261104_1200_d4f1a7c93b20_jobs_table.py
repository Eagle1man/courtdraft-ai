"""Таблица jobs — состояние фоновых задач переживает перезапуск

Revision ID: d4f1a7c93b20
Revises: c71d5aa40e63
Create Date: 2026-11-04 12:00:00.000000

Зачем таблица: задания генерации жили только в памяти процесса. При
перезапуске службы (а на живом тесте их было пять) незавершённые
задания терялись — около 34 за тест, — а клиент продолжал опрашивать
исчезнувший job_id и видел вечное «в очереди».

Теперь задача записывается в базу. После старта служба помечает
незавершённые задания как прерванные: это факт (процесс остановлен),
а не догадка, поэтому клиент получает причину, а не пустоту.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f1a7c93b20"
down_revision: str | None = "c71d5aa40e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("result_draft_id", sa.Uuid(), nullable=True),
        sa.Column("result_version", sa.Integer(), nullable=True),
        sa.Column(
            "verification_status",
            sa.String(length=30),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_jobs_case_id", "jobs", ["case_id"])
    op.create_index("ix_jobs_state", "jobs", ["state"])


def downgrade() -> None:
    op.drop_index("ix_jobs_state", table_name="jobs")
    op.drop_index("ix_jobs_case_id", table_name="jobs")
    op.drop_table("jobs")
