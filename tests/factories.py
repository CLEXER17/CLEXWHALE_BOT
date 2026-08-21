"""Builders for Hyperliquid domain objects.

Deliberately verbose defaults: every factory produces a *plausible* object with
realistic prices and sizes so a test only has to state the one thing it is
actually about. These are test fixtures for shapes the real API returns — the
production code never generates market data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any

from app.hyperliquid.models import (
    AccountState,
    BookLevel,
    L2Book,
    OpenOrder,
    OrderUpdate,
    Position,
    Trade,
)
from app.whale.detector import OrderState, PositionContext

WALLET_A = "0x1111111111111111111111111111111111111111"
WALLET_B = "0x2222222222222222222222222222222222222222"
WALLET_C = "0x3333333333333333333333333333333333333333"

BTC_PX = 100_000.0

_oids = count(9_000_001)
_tids = count(5_000_001)


def now() -> datetime:
    return datetime.now(timezone.utc)


def ago(seconds: float) -> datetime:
    return now() - timedelta(seconds=seconds)


# ── market ─────────────────────────────────────────────────────

def make_trade(
    *,
    coin: str = "BTC",
    px: float = BTC_PX,
    sz: float = 50.0,
    side: str = "B",
    buyer: str | None = WALLET_A,
    seller: str | None = WALLET_B,
    tid: int | None = None,
    when: datetime | None = None,
) -> Trade:
    """``side`` is the taker side: ``"B"`` buyer-aggressor, ``"A"`` seller."""
    return Trade(
        coin=coin,
        side=side,
        px=px,
        sz=sz,
        time=when or now(),
        tid=tid if tid is not None else next(_tids),
        hash="0x" + "ab" * 32,
        buyer=buyer,
        seller=seller,
    )


def make_book(
    *,
    coin: str = "BTC",
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
) -> L2Book:
    return L2Book(
        coin=coin,
        time=now(),
        bids=[BookLevel(px, sz, 3) for px, sz in (bids or [])],
        asks=[BookLevel(px, sz, 2) for px, sz in (asks or [])],
    )


# ── positions ──────────────────────────────────────────────────

def make_position(
    *,
    coin: str = "BTC",
    szi: float = 60.0,
    entry_px: float = 98_000.0,
    position_value: float | None = None,
    liquidation_px: float | None = 74_500.0,
    leverage_value: float | None = 5.0,
    leverage_type: str = "cross",
    margin_used: float | None = 1_176_000.0,
    unrealized_pnl: float | None = 120_000.0,
    max_leverage: float | None = 40.0,
) -> Position:
    return Position(
        coin=coin,
        szi=szi,
        entry_px=entry_px,
        position_value=position_value if position_value is not None else abs(szi) * entry_px,
        unrealized_pnl=unrealized_pnl,
        liquidation_px=liquidation_px,
        margin_used=margin_used,
        max_leverage=max_leverage,
        leverage_value=leverage_value,
        leverage_type=leverage_type,
    )


def make_account_state(
    *,
    user: str = WALLET_A,
    positions: list[Position] | None = None,
    account_value: float = 12_000_000.0,
) -> AccountState:
    items = positions if positions is not None else [make_position()]
    return AccountState(
        user=user,
        positions={p.coin: p for p in items},
        account_value=account_value,
        total_ntl_pos=sum(p.notional for p in items),
        total_margin_used=sum(p.margin_used or 0.0 for p in items),
        withdrawable=account_value * 0.5,
        time=now(),
    )


def make_context(
    *,
    position: Position | None = None,
    trigger_orders: list[OpenOrder] | None = None,
    orders_known: bool = True,
    account_value: float | None = 12_000_000.0,
    first_seen: datetime | None = None,
) -> PositionContext:
    return PositionContext(
        position=position if position is not None else make_position(),
        trigger_orders=trigger_orders or [],
        account_value=account_value,
        first_seen=first_seen,
        orders_known=orders_known,
    )


# ── orders ─────────────────────────────────────────────────────

def make_open_order(
    *,
    coin: str = "BTC",
    oid: int | None = None,
    side: str = "B",
    limit_px: float | None = 95_000.0,
    sz: float = 40.0,
    orig_sz: float | None = None,
    order_type: str | None = "Limit",
    is_trigger: bool = False,
    trigger_px: float | None = None,
    reduce_only: bool = False,
    is_position_tpsl: bool = False,
    children: list[OpenOrder] | None = None,
    when: datetime | None = None,
) -> OpenOrder:
    return OpenOrder(
        coin=coin,
        oid=oid if oid is not None else next(_oids),
        side=side,
        limit_px=limit_px,
        sz=sz,
        orig_sz=orig_sz if orig_sz is not None else sz,
        timestamp=when or now(),
        order_type=order_type,
        reduce_only=reduce_only,
        is_trigger=is_trigger,
        trigger_px=trigger_px,
        trigger_condition="tp/sl" if is_trigger else None,
        is_position_tpsl=is_position_tpsl,
        tif="Gtc",
        children=children or [],
    )


def make_take_profit(px: float = 115_000.0, *, sz: float = 60.0, market: bool = True) -> OpenOrder:
    """A real trader-set TP: a reduce-only trigger order Hyperliquid labels."""
    return make_open_order(
        side="A",
        limit_px=px,
        sz=sz,
        order_type="Take Profit Market" if market else "Take Profit Limit",
        is_trigger=True,
        trigger_px=px,
        reduce_only=True,
        is_position_tpsl=True,
    )


def make_stop_loss(px: float = 88_000.0, *, sz: float = 60.0, market: bool = True) -> OpenOrder:
    return make_open_order(
        side="A",
        limit_px=px,
        sz=sz,
        order_type="Stop Market" if market else "Stop Limit",
        is_trigger=True,
        trigger_px=px,
        reduce_only=True,
        is_position_tpsl=True,
    )


def make_order_update(
    *,
    coin: str = "BTC",
    oid: int = 9_100_001,
    side: str = "B",
    limit_px: float | None = 95_000.0,
    sz: float = 40.0,
    orig_sz: float | None = None,
    status: str = "open",
) -> OrderUpdate:
    return OrderUpdate(
        coin=coin,
        oid=oid,
        side=side,
        limit_px=limit_px,
        sz=sz,
        orig_sz=orig_sz if orig_sz is not None else sz,
        timestamp=now(),
        status=status,
        status_timestamp=now(),
    )


def make_order_state(
    *,
    coin: str = "BTC",
    oid: int = 9_100_001,
    side: str = "B",
    limit_px: float | None = 95_000.0,
    size: float = 40.0,
    orig_size: float | None = None,
    status: str = "open",
    is_trigger: bool = False,
    order_type: str | None = "Limit",
    reduce_only: bool = False,
) -> OrderState:
    price = limit_px or 0.0
    return OrderState(
        oid=oid,
        coin=coin,
        side=side,
        limit_px=limit_px,
        size=size,
        orig_size=orig_size if orig_size is not None else size,
        notional=abs(price * size),
        status=status,
        is_trigger=is_trigger,
        trigger_px=None,
        order_type=order_type,
        reduce_only=reduce_only,
        placed_at=ago(120),
    )


# ── raw websocket frames ───────────────────────────────────────

def raw_trade(
    *,
    coin: str = "BTC",
    px: float = BTC_PX,
    sz: float = 50.0,
    side: str = "B",
    users: list[str] | None = None,
    tid: int | None = None,
) -> dict[str, Any]:
    """The exact shape Hyperliquid pushes on the ``trades`` channel."""
    return {
        "coin": coin,
        "side": side,
        "px": str(px),
        "sz": str(sz),
        "time": int(now().timestamp() * 1000),
        "tid": tid if tid is not None else next(_tids),
        "hash": "0x" + "cd" * 32,
        "users": users if users is not None else [WALLET_A, WALLET_B],
    }


def price_map(**prices: float) -> dict[str, float]:
    return {"BTC": BTC_PX, "ETH": 4_000.0, "SOL": 200.0, **prices}
