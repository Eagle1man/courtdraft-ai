"""student_code в cases — изоляция дел студента

Revision ID: 9b3e64d1c7aa
Revises: 4a19f2c7e8b1
Create Date: 2026-11-03 12:00:00.000000

Зачем поле: код участника раньше писался только в журнал аудита, а
колонки в `cases` не было. Из-за этого список дел отдавал дела всех
студентов сразу: фильтровать было не по чему, и каждый видел чужие
материалы.

Пустая строка — не «неизвестно», а анонимное дело: демо-дело видят все,
иначе стенд нечем открыть.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b3e64d1c7aa"
down_revision: str | None = "4a19f2c7e8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default обязателен: в таблице уже есть строки, а nullable=False
    # без значения по умолчанию обрушил бы миграцию на реальных данных.
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "student_code",
                sa.String(length=100),
                nullable=False,
                server_default="",
            )
        )
        batch_op.create_index("ix_cases_student_code", ["student_code"])


def downgrade() -> None:
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.drop_index("ix_cases_student_code")
        batch_op.drop_column("student_code")
