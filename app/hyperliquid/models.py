"""Typed views over Hyperliquid API payloads.

Every field maps 1:1 onto a documented response field. Nothing is invented:
if Hyperliquid omits a value (``liquidationPx`` on a fully-margined cross
position, ``triggerPx`` on a plain limit order) the attribute stays ``None``
and the renderer prints it as unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.hyperliquid.constants import TriggerKind, classify_trigger, side_label
from app.utils.formatting import to_float


# ── market data ────────────────────────────────────────────────

@dataclass(slots=True)
class AssetMeta:
    name: str
    sz_decimals: int = 0
    max_leverage: float | None = None
    only_isolated: bool = False


@dataclass(slots=True)
class AssetContext:
    coin: str
    mark_px: float | None = None
    mid_px: float | None = None
    oracle_px: float | None = None
    prev_day_px: float | None = None
    funding: float | None = None
    open_interest: float | None = None
    day_ntl_vlm: float | None = None

    @property
    def reference_price(self) -> float | None:
        """Best available "current price": mark → mid → oracle."""
        return self.mark_px or self.mid_px or self.oracle_px


@dataclass(slots=True)
class Trade:
    """One executed trade from the ``trades`` websocket feed.

    ``side`` is the taker side. ``users`` is documented as ``[buyer, seller]``.
    """

    coin: str
    side: str | None
    px: float
    sz: float
    time: datetime | None
    tid: int | None = None
    hash: str | None = None
    buyer: str | None = None
    seller: str | None = None

    @property
    def notional(self) -> float:
        return abs(self.px * self.sz)

    @property
    def taker_side(self) -> str | None:
        return side_label(self.side)

    @property
    def taker(self) -> str | None:
        """The aggressor address, derived from the taker side."""
        if self.side == "B":
            return self.buyer
        if self.side == "A":
            return self.seller
        return None

    @property
    def maker(self) -> str | None:
        if self.side == "B":
            return self.seller
        if self.side == "A":
            return self.buyer
        return None

    @property
    def participants(self) -> tuple[str, ...]:
        return tuple(u for u in (self.buyer, self.seller) if u)


@dataclass(slots=True)
class BookLevel:
    px: float
    sz: float
    n: int = 0

    @property
    def notional(self) -> float:
        return abs(self.px * self.sz)


@dataclass(slots=True)
class L2Book:
    coin: str
    time: datetime | None
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)


# ── account data ───────────────────────────────────────────────

@dataclass(slots=True)
class Position:
    coin: str
    szi: float
    entry_px: float | None = None
    position_value: float | None = None
    unrealized_pnl: float | None = None
    return_on_equity: float | None = None
    liquidation_px: float | None = None
    margin_used: float | None = None
    max_leverage: float | None = None
    leverage_value: float | None = None
    leverage_type: str | None = None
    funding_since_open: float | None = None

    @property
    def is_long(self) -> bool:
        return self.szi > 0

    @property
    def is_flat(self) -> bool:
        """No position. Hyperliquid drops closed positions from
        ``assetPositions``, but a snapshot taken mid-close can still carry
        ``szi == 0``."""
        return self.szi == 0

    @property
    def side(self) -> str | None:
        """``LONG``, ``SHORT``, or ``None`` when the position is flat.

        A flat position has no side, and saying "SHORT" for one would be a
        fabrication with consequences: a closing snapshot arrives with ``szi == 0``,
        and reading a side off it would label every closed LONG as a SHORT. Callers
        that need the side of a position that has just closed must read it from the
        last non-zero snapshot instead.
        """
        if self.szi > 0:
            return "LONG"
        if self.szi < 0:
            return "SHORT"
        return None

    @property
    def abs_size(self) -> float:
        return abs(self.szi)

    @property
    def notional(self) -> float:
        if self.position_value is not None:
            return abs(self.position_value)
        if self.entry_px:
            return abs(self.szi * self.entry_px)
        return 0.0


@dataclass(slots=True)
class AccountState:
    user: str
    positions: dict[str, Position] = field(default_factory=dict)
    account_value: float | None = None
    total_ntl_pos: float | None = None
    total_margin_used: float | None = None
    withdrawable: float | None = None
    time: datetime | None = None

    def position(self, coin: str) -> Position | None:
        return self.positions.get(coin)


@dataclass(slots=True)
class OpenOrder:
    """A resting order from ``frontendOpenOrders``.

    ``frontendOpenOrders`` is the richer of the two order endpoints: it is the
    only public source of a trader's real TP/SL levels (``isTrigger`` +
    ``triggerPx`` + ``orderType``).
    """

    coin: str
    oid: int
    side: str | None
    limit_px: float | None
    sz: float
    orig_sz: float | None = None
    timestamp: datetime | None = None
    order_type: str | None = None
    reduce_only: bool = False
    is_trigger: bool = False
    trigger_px: float | None = None
    trigger_condition: str | None = None
    is_position_tpsl: bool = False
    tif: str | None = None
    cloid: str | None = None
    #: Attached TP/SL legs Hyperliquid nests under a parent order.
    children: list["OpenOrder"] = field(default_factory=list)

    @property
    def direction(self) -> str | None:
        return side_label(self.side)

    @property
    def price(self) -> float | None:
        """Trigger orders are priced off ``triggerPx``; limits off ``limitPx``."""
        if self.is_trigger and self.trigger_px:
            return self.trigger_px
        return self.limit_px

    @property
    def notional(self) -> float:
        price = self.price
        return abs(price * self.sz) if price else 0.0

    @property
    def filled_sz(self) -> float | None:
        if self.orig_sz is None:
            return None
        return max(0.0, self.orig_sz - self.sz)

    @property
    def trigger_kind(self) -> TriggerKind | None:
        if not self.is_trigger:
            return None
        return classify_trigger(self.order_type)


@dataclass(slots=True)
class OrderUpdate:
    """One entry of the ``orderUpdates`` websocket feed."""

    coin: str
    oid: int
    side: str | None
    limit_px: float | None
    sz: float
    orig_sz: float | None
    timestamp: datetime | None
    status: str
    status_timestamp: datetime | None
    cloid: str | None = None

    @property
    def direction(self) -> str | None:
        return side_label(self.side)

    @property
    def notional(self) -> float:
        return abs(self.limit_px * self.sz) if self.limit_px else 0.0

    @property
    def original_notional(self) -> float | None:
        if self.limit_px is None or self.orig_sz is None:
            return None
        return abs(self.limit_px * self.orig_sz)

    @property
    def filled_sz(self) -> float | None:
        if self.orig_sz is None:
            return None
        return max(0.0, self.orig_sz - self.sz)


@dataclass(slots=True)
class Fill:
    coin: str
    px: float
    sz: float
    side: str | None
    time: datetime | None
    oid: int | None = None
    tid: int | None = None
    hash: str | None = None
    dir: str | None = None
    closed_pnl: float | None = None
    start_position: float | None = None
    crossed: bool = False
    fee: float | None = None
    liquidation: dict[str, Any] | None = None

    @property
    def notional(self) -> float:
        return abs(self.px * self.sz)

    @property
    def is_liquidation(self) -> bool:
        return self.liquidation is not None
