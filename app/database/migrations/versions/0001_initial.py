"""initial schema

Every table the bot needs, written dialect-neutrally so the same revision runs
on Railway's PostgreSQL and on SQLite for local development. Autoincrement
primary keys and Telegram ids use ``BigInteger`` with an ``Integer`` variant on
SQLite, matching :mod:`app.database.models`.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Same variants as the ORM, so autogenerate never sees a spurious diff.
_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_TG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _created() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _updated() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    # ── admins ────────────────────────────────────────────────
    op.create_table(
        "admins",
        sa.Column("id", _PK, primary_key=True, autoincrement=True),
        sa.Column("telegram_id", _TG_ID, nullable=False),
        sa.Column("username", sa.String(length=64)),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("added_by", _TG_ID),
        sa.Column("note", sa.String(length=256)),
        _created(),
        _updated(),
    )
    op.create_index("ix_admins_telegram_id", "admins", ["telegram_id"], unique=True)

    # ── users ─────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("telegram_id", _TG_ID, primary_key=True),
        sa.Column("chat_id", _TG_ID, nullable=False),
        sa.Column("username", sa.String(length=64)),
        sa.Column("first_name", sa.String(length=128)),
        sa.Column("is_subscribed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("alerts_received", sa.Integer(), nullable=False, server_default="0"),
        _created(),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ── settings ──────────────────────────────────────────────
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_by", _TG_ID),
        _updated(),
    )

    # ── coin / wallet selection ───────────────────────────────
    op.create_table(
        "tracked_coins",
        sa.Column("coin", sa.String(length=32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("added_by", _TG_ID),
        _created(),
    )
    op.create_table(
        "tracked_wallets",
        sa.Column("address", sa.String(length=42), primary_key=True),
        sa.Column("label", sa.String(length=64)),
        sa.Column("added_by", _TG_ID),
        _created(),
    )

    # ── observed wallets ──────────────────────────────────────
    op.create_table(
        "wallets",
        sa.Column("address", sa.String(length=42), primary_key=True),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("long_volume", sa.Float(), nullable=False, server_default="0"),
        sa.Column("short_volume", sa.Float(), nullable=False, server_default="0"),
        sa.Column("largest_position", sa.Float(), nullable=False, server_default="0"),
        sa.Column("largest_order", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_notional", sa.Float(), nullable=False, server_default="0"),
        sa.Column("account_value", sa.Float()),
        sa.Column("coins", sa.JSON(), nullable=False),
    )

    # ── whale events ──────────────────────────────────────────
    op.create_table(
        "whale_events",
        sa.Column("id", _PK, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("coin", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8)),
        sa.Column("wallet", sa.String(length=42)),
        sa.Column("notional", sa.Float(), nullable=False),
        sa.Column("value_kind", sa.String(length=24), nullable=False),
        sa.Column("price", sa.Float()),
        sa.Column("size", sa.Float()),
        sa.Column("entry_px", sa.Float()),
        sa.Column("liquidation_px", sa.Float()),
        sa.Column("leverage", sa.Float()),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("stop_loss_px", sa.Float()),
        sa.Column("position_value", sa.Float()),
        sa.Column("order_id", sa.BigInteger()),
        sa.Column("status", sa.String(length=48)),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        _created(),
        sa.Column("alerted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_whale_events_event_type", "whale_events", ["event_type"])
    op.create_index("ix_whale_events_coin", "whale_events", ["coin"])
    op.create_index("ix_whale_events_wallet", "whale_events", ["wallet"])
    op.create_index("ix_whale_events_notional", "whale_events", ["notional"])
    op.create_index("ix_whale_events_dedup_key", "whale_events", ["dedup_key"])
    op.create_index("ix_whale_events_event_time", "whale_events", ["event_time"])
    op.create_index("ix_whale_events_coin_time", "whale_events", ["coin", "event_time"])
    op.create_index("ix_whale_events_type_time", "whale_events", ["event_type", "event_time"])

    # ── orders ────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", _PK, primary_key=True, autoincrement=True),
        sa.Column("oid", sa.BigInteger(), nullable=False),
        sa.Column("wallet", sa.String(length=42), nullable=False),
        sa.Column("coin", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8)),
        sa.Column("limit_px", sa.Float()),
        sa.Column("size", sa.Float()),
        sa.Column("orig_size", sa.Float()),
        sa.Column("notional", sa.Float(), nullable=False, server_default="0"),
        sa.Column("orig_notional", sa.Float()),
        sa.Column("order_type", sa.String(length=32)),
        sa.Column("is_trigger", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trigger_px", sa.Float()),
        sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=48), nullable=False, server_default="open"),
        sa.Column("placed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        _created(),
        _updated(),
        sa.UniqueConstraint("wallet", "oid", name="uq_orders_wallet_oid"),
    )
    op.create_index("ix_orders_oid", "orders", ["oid"])
    op.create_index("ix_orders_wallet", "orders", ["wallet"])
    op.create_index("ix_orders_coin", "orders", ["coin"])

    # ── positions ─────────────────────────────────────────────
    op.create_table(
        "positions",
        sa.Column("id", _PK, primary_key=True, autoincrement=True),
        sa.Column("wallet", sa.String(length=42), nullable=False),
        sa.Column("coin", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8)),
        sa.Column("size", sa.Float()),
        sa.Column("entry_px", sa.Float()),
        sa.Column("position_value", sa.Float()),
        sa.Column("liquidation_px", sa.Float()),
        sa.Column("leverage", sa.Float()),
        sa.Column("leverage_type", sa.String(length=16)),
        sa.Column("margin_used", sa.Float()),
        sa.Column("unrealized_pnl", sa.Float()),
        sa.Column("take_profit_px", sa.Float()),
        sa.Column("stop_loss_px", sa.Float()),
        sa.Column("max_notional", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        _updated(),
        sa.UniqueConstraint("wallet", "coin", name="uq_positions_wallet_coin"),
    )
    op.create_index("ix_positions_wallet", "positions", ["wallet"])
    op.create_index("ix_positions_coin", "positions", ["coin"])

    # ── alert history ─────────────────────────────────────────
    op.create_table(
        "alert_history",
        sa.Column("id", _PK, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger()),
        sa.Column("dedup_key", sa.String(length=128), nullable=False),
        sa.Column("chat_id", _TG_ID),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.String(length=256)),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_alert_history_event_id", "alert_history", ["event_id"])
    op.create_index("ix_alert_history_dedup_key", "alert_history", ["dedup_key"])

    # ── audit trail ───────────────────────────────────────────
    op.create_table(
        "admin_audit",
        sa.Column("id", _PK, primary_key=True, autoincrement=True),
        sa.Column("admin_id", _TG_ID, nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("target", sa.String(length=128)),
        sa.Column("old_value", sa.String(length=512)),
        sa.Column("new_value", sa.String(length=512)),
        _created(),
    )
    op.create_index("ix_admin_audit_admin_id", "admin_audit", ["admin_id"])

    # ── operational log ───────────────────────────────────────
    op.create_table(
        "bot_logs",
        sa.Column("id", _PK, primary_key=True, autoincrement=True),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=512), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        _created(),
    )


def downgrade() -> None:
    op.drop_table("bot_logs")
    op.drop_index("ix_admin_audit_admin_id", table_name="admin_audit")
    op.drop_table("admin_audit")
    op.drop_index("ix_alert_history_dedup_key", table_name="alert_history")
    op.drop_index("ix_alert_history_event_id", table_name="alert_history")
    op.drop_table("alert_history")
    op.drop_index("ix_positions_coin", table_name="positions")
    op.drop_index("ix_positions_wallet", table_name="positions")
    op.drop_table("positions")
    op.drop_index("ix_orders_coin", table_name="orders")
    op.drop_index("ix_orders_wallet", table_name="orders")
    op.drop_index("ix_orders_oid", table_name="orders")
    op.drop_table("orders")
    for index in (
        "ix_whale_events_type_time",
        "ix_whale_events_coin_time",
        "ix_whale_events_event_time",
        "ix_whale_events_dedup_key",
        "ix_whale_events_notional",
        "ix_whale_events_wallet",
        "ix_whale_events_coin",
        "ix_whale_events_event_type",
    ):
        op.drop_index(index, table_name="whale_events")
    op.drop_table("whale_events")
    op.drop_table("wallets")
    op.drop_table("tracked_wallets")
    op.drop_table("tracked_coins")
    op.drop_table("settings")
    op.drop_table("users")
    op.drop_index("ix_admins_telegram_id", table_name="admins")
    op.drop_table("admins")
