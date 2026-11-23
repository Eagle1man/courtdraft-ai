"""Паспорт дела, журнал расхождений, разделение проектов по виду документа.

Revision ID: 9c2f1a7d4b60
Revises: c0d1e2f3a4b5
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c2f1a7d4b60"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_passports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schema_id", sa.String(40), nullable=False, server_default="case-passport/3.1"),
        sa.Column("mode", sa.String(10), nullable=False, server_default="ETALON"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ok"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="upload"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("case_id", name="uq_case_passports_case"),
    )
    op.create_index("ix_case_passports_case", "case_passports", ["case_id"])

    op.create_table(
        "case_deviations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="P1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_case_deviations_case", "case_deviations", ["case_id"])

    op.execute(
        "UPDATE drafts SET document_kind = 'claim' "
        "WHERE template_name = 'civil_claim' AND (document_kind IS NULL OR document_kind = 'judgment')"
    )
    op.create_index(
        "uq_drafts_case_kind", "drafts", ["case_id", "document_kind"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_drafts_case_kind", table_name="drafts")
    op.drop_index("ix_case_deviations_case", table_name="case_deviations")
    op.drop_table("case_deviations")
    op.drop_index("ix_case_passports_case", table_name="case_passports")
    op.drop_table("case_passports")