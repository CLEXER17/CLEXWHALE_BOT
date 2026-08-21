"""alert reply threading

Adds ``alert_history.thread_key`` so a follow-up alert can be delivered as a
Telegram reply to the first message of the same thread (one thread per
wallet+coin). Nullable and indexed: existing rows keep ``NULL`` and simply start
a new thread the next time that wallet is reported, so the upgrade needs no
backfill and the downgrade loses nothing but the grouping.

Revision ID: 0002_alert_thread_key
Revises: 0001_initial
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_alert_thread_key"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("alert_history", sa.Column("thread_key", sa.String(length=96), nullable=True))
    op.create_index(
        "ix_alert_history_thread_key", "alert_history", ["thread_key"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_alert_history_thread_key", table_name="alert_history")
    op.drop_column("alert_history", "thread_key")
