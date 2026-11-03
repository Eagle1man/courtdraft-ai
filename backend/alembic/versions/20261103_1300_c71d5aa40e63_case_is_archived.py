"""is_archived в cases — скрыть прежние дела из интерфейса

Revision ID: c71d5aa40e63
Revises: 9b3e64d1c7aa
Create Date: 2026-11-03 13:00:00.000000

Зачем поле: на стенде накопились дела, созданные до появления кодов
участника. У них нет владельца, поэтому фильтр по коду их не отсекает:
анонимное дело видно всем. Это и есть утечка, ради которой делалась
изоляция.

Удалять чужие данные миграция не вправе, поэтому прежние дела
помечаются скрытыми: строки остаются в базе (их видно в выгрузке и в
резервной копии), но интерфейс их не показывает и по прямой ссылке
отдаёт 404.

Скрытие выполняется один раз, в момент миграции: у дел, созданных
после, признак по умолчанию ложный. Повторного запуска не будет —
поэтому новые дела под архивацию не попадут.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c71d5aa40e63"
down_revision: str | None = "9b3e64d1c7aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default обязателен: в таблице уже есть строки, а nullable=False
    # без значения по умолчанию обрушил бы миграцию на реальных данных.
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_archived",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index("ix_cases_is_archived", ["is_archived"])

    # Всё, что существовало до этой миграции, — прежние дела без владельца.
    op.execute(sa.text("UPDATE cases SET is_archived = true"))


def downgrade() -> None:
    with op.batch_alter_table("cases", schema=None) as batch_op:
        batch_op.drop_index("ix_cases_is_archived")
        batch_op.drop_column("is_archived")
