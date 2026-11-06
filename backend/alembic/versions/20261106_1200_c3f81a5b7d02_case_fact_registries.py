"""Реестр фактов дела: снимок извлечения и проверки источников

Revision ID: c3f81a5b7d02
Revises: a1c7f3e9d240
Create Date: 2026-11-06 12:00:00.000000

Зачем таблица: извлечение фактов — это вызовы модели по каждому
документу, минуты работы. Пересчитывать это на каждый показ карточки
нельзя, а объяснить, откуда взялся факт в прошлом прогоне, нужно.

Полный реестр лежит в JSON: он относится к конкретному состоянию
материалов и меняется вместе с моделью извлечения, поэтому раскладывать
его по колонкам пришлось бы миграцией на каждое новое поле. Колонки
`status` и `documents` вынесены отдельно — по ним строится список и
видно, полон ли охват.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f81a5b7d02"
down_revision: str | None = "a1c7f3e9d240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_fact_registries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("documents", sa.Integer(), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_case_fact_registries_case_id", "case_fact_registries", ["case_id"]
    )
    op.create_index(
        "ix_case_fact_registries_status", "case_fact_registries", ["status"]
    )

    # Вид задания: до этой миграции все задания были формированием проекта.
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column(
                "kind", sa.String(length=20), nullable=False, server_default="draft"
            )
        )
        batch.create_index("ix_jobs_kind", ["kind"])


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_kind")
        batch.drop_column("kind")

    op.drop_index("ix_case_fact_registries_status", table_name="case_fact_registries")
    op.drop_index("ix_case_fact_registries_case_id", table_name="case_fact_registries")
    op.drop_table("case_fact_registries")
