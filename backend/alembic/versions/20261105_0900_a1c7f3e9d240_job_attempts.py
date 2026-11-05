"""Счётчик попыток задания — защита от бесконечного возобновления

Revision ID: a1c7f3e9d240
Revises: d4f1a7c93b20
Create Date: 2026-11-05 09:00:00.000000

Зачем колонка: после перезапуска служба снова берётся за незавершённые
задания, а не просто помечает их прерванными. Но задание, которое само
валит процесс (нехватка памяти, зависшая модель), возобновлялось бы
после каждого старта и снова валило бы службу — перезапуск превратился
бы в цикл. Счётчик делает возобновление конечным: одна повторная
попытка, после неё задание честно остаётся проваленным с причиной.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7f3e9d240"
down_revision: str | None = "d4f1a7c93b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("attempts")
