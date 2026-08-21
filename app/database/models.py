"""SQLAlchemy models. All timestamps are timezone-aware UTC.

Everything an administrator can change through Telegram lives here rather than
in memory, so a Railway restart or redeploy restores: admins, co-admins, public
mode, monitoring state, thresholds, coin selection, alert/cooldown settings,
tracked wallets and historical whale events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base

#: Autoincrement PK that is BIGSERIAL on Postgres and INTEGER on SQLite.
_PK = BigInteger().with_variant(Integer, "sqlite")
#: Telegram ids exceed 2^31 — always 64-bit.
_TG_ID = BigInteger().with_variant(Integer, "sqlite")


def _now_column(**kwargs: Any) -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, **kwargs
    )


class Admin(Base):
    """Main admin and co-admins. Telegram id is the permanent identifier."""

    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(_TG_ID, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="CO_ADMIN")
    added_by: Mapped[int | None] = mapped_column(_TG_ID)
    note: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(Base):
    """Everyone who has interacted with the bot, plus their alert preference."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(_TG_ID, primary_key=True)
    chat_id: Mapped[int] = mapped_column(_TG_ID, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    is_subscribed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    alerts_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _now_column()
    last_seen_at: Mapped[datetime] = _now_column()


class Setting(Base):
    """Runtime configuration, JSON-encoded. Seeded from env on first boot."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(_TG_ID)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TrackedCoin(Base):
    __tablename__ = "tracked_coins"

    coin: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_by: Mapped[int | None] = mapped_column(_TG_ID)
    created_at: Mapped[datetime] = _now_column()


class TrackedWallet(Base):
    """Wallets pinned by an admin (``/watch``) — always monitored."""

    __tablename__ = "tracked_wallets"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(64))
    added_by: Mapped[int | None] = mapped_column(_TG_ID)
    created_at: Mapped[datetime] = _now_column()


class Wallet(Base):
    """Aggregate statistics for every trader we have observed.

    Identity claims are deliberately absent: an address is an address. Labels
    are operator-supplied only, never inferred.
    """

    __tablename__ = "wallets"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    first_seen: Mapped[datetime] = _now_column()
    last_seen: Mapped[datetime] = _now_column()
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    long_volume: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    short_volume: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    largest_position: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    largest_order: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_notional: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    account_value: Mapped[float | None] = mapped_column(Float)
    coins: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WhaleEvent(Base):
    __tablename__ = "whale_events"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    coin: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str | None] = mapped_column(String(8))
    wallet: Mapped[str | None] = mapped_column(String(42), index=True)
    #: The value the threshold was applied to.
    notional: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    #: Which metric ``notional`` is — trade value, position notional, ... .
    value_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    price: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float | None] = mapped_column(Float)
    entry_px: Mapped[float | None] = mapped_column(Float)
    liquidation_px: Mapped[float | None] = mapped_column(Float)
    leverage: Mapped[float | None] = mapped_column(Float)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    stop_loss_px: Mapped[float | None] = mapped_column(Float)
    position_value: Mapped[float | None] = mapped_column(Float)
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str | None] = mapped_column(String(48))
    #: Full DataPoint map (value + confidence + note) for audit / replay.
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = _now_column()
    alerted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_whale_events_coin_time", "coin", "event_time"),
        Index("ix_whale_events_type_time", "event_type", "event_time"),
    )


class OrderRecord(Base):
    """Lifecycle state of a large resting order (one row per oid+wallet)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    oid: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    wallet: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    coin: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str | None] = mapped_column(String(8))
    limit_px: Mapped[float | None] = mapped_column(Float)
    size: Mapped[float | None] = mapped_column(Float)
    orig_size: Mapped[float | None] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    orig_notional: Mapped[float | None] = mapped_column(Float)
    order_type: Mapped[str | None] = mapped_column(String(32))
    is_trigger: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trigger_px: Mapped[float | None] = mapped_column(Float)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(48), default="open", nullable=False)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("wallet", "oid", name="uq_orders_wallet_oid"),)


class PositionRecord(Base):
    """Latest known state of a tracked wallet's position, plus its lifetime."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    coin: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str | None] = mapped_column(String(8))
    size: Mapped[float | None] = mapped_column(Float)
    entry_px: Mapped[float | None] = mapped_column(Float)
    position_value: Mapped[float | None] = mapped_column(Float)
    liquidation_px: Mapped[float | None] = mapped_column(Float)
    leverage: Mapped[float | None] = mapped_column(Float)
    leverage_type: Mapped[str | None] = mapped_column(String(16))
    margin_used: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    take_profit_px: Mapped[float | None] = mapped_column(Float)
    stop_loss_px: Mapped[float | None] = mapped_column(Float)
    max_notional: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("wallet", "coin", name="uq_positions_wallet_coin"),)


class AlertHistory(Base):
    __tablename__ = "alert_history"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chat_id: Mapped[int | None] = mapped_column(_TG_ID)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[str | None] = mapped_column(String(256))
    sent_at: Mapped[datetime] = _now_column()


class AdminAudit(Base):
    """Every privileged action, for after-the-fact review."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(_TG_ID, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target: Mapped[str | None] = mapped_column(String(128))
    old_value: Mapped[str | None] = mapped_column(String(512))
    new_value: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = _now_column()


class BotLog(Base):
    """Operational breadcrumbs worth keeping across restarts (never secrets)."""

    __tablename__ = "bot_logs"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = _now_column()
