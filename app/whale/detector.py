"""Whale detection.

This module is deliberately I/O-free: it turns Hyperliquid data structures into
:class:`~app.whale.events.WhaleEvent` objects and nothing else. Fetching,
polling and subscription management live in :mod:`app.whale.tracker` and
:mod:`app.whale.engine`, which makes every rule here directly unit-testable.

What is real, and where it comes from
-------------------------------------
======================  ==========================================  ============
Field                   Source                                      Confidence
======================  ==========================================  ============
Trade value             ``trades`` feed ``px * sz``                  confirmed
Wallet address          ``trades`` feed ``users[buyer, seller]``     confirmed
Direction (trade)       ``trades`` feed ``side`` (taker side)        confirmed
Position side / size    ``clearinghouseState`` ``szi``               confirmed
Entry price             ``clearinghouseState`` ``entryPx``           confirmed
Position notional       ``clearinghouseState`` ``positionValue``     confirmed
Liquidation price       ``clearinghouseState`` ``liquidationPx``     confirmed
Leverage                ``clearinghouseState`` ``leverage``          confirmed
Margin used             ``clearinghouseState`` ``marginUsed``        confirmed
Order price / size      ``frontendOpenOrders`` / ``orderUpdates``    confirmed
Order terminal status   ``orderStatus`` by oid                       confirmed
Take profit / stop      ``frontendOpenOrders`` trigger orders        confirmed
Position change value   ``|Δszi| × mark price``                      estimated
Book level notional     ``l2Book`` (aggregate, no wallet)            confirmed
Holding time            first observation by this bot                estimated
======================  ==========================================  ============

Anything absent from that table is reported as unavailable, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Sequence

from app.hyperliquid.constants import (
    TriggerKind,
    is_cancel_status,
    is_reject_status,
    side_label,
    status_label,
)
from app.hyperliquid.models import L2Book, OpenOrder, OrderUpdate, Position, Trade
from app.utils.formatting import DataPoint, pct_distance, utc_now
from app.whale.events import EventType, ValueKind, WhaleEvent

PriceProvider = Callable[[str], float | None]

#: Positions below this absolute size are treated as flat (dust from rounding).
DUST = 1e-12


@dataclass(slots=True)
class PositionContext:
    """Everything we know about a wallet's position at detection time."""

    position: Position | None = None
    #: Resting trigger orders for this coin, from ``frontendOpenOrders``.
    trigger_orders: list[OpenOrder] = field(default_factory=list)
    account_value: float | None = None
    #: When *this bot* first observed the position — not when it was opened.
    first_seen: datetime | None = None
    #: True when ``trigger_orders`` reflects a successful fetch (so "no TP/SL
    #: found" is meaningful rather than "we never looked").
    orders_known: bool = False


@dataclass(slots=True)
class OrderState:
    """Previously observed state of a resting order, for diffing."""

    oid: int
    coin: str
    side: str | None
    limit_px: float | None
    size: float
    orig_size: float | None
    notional: float
    status: str = "open"
    is_trigger: bool = False
    trigger_px: float | None = None
    order_type: str | None = None
    reduce_only: bool = False
    placed_at: datetime | None = None


def _flatten(orders: Sequence[OpenOrder]) -> list[OpenOrder]:
    """Orders plus any TP/SL legs Hyperliquid nests under ``children``."""
    out: list[OpenOrder] = []
    for order in orders:
        out.append(order)
        if order.children:
            out.extend(_flatten(order.children))
    return out


def extract_tpsl(
    orders: Sequence[OpenOrder],
    coin: str,
    position_side: str | None,
    current_px: float | None = None,
) -> tuple[DataPoint, DataPoint]:
    """Pull real take-profit / stop-loss levels out of resting trigger orders.

    ``frontendOpenOrders`` is the only public source for this: an ``orderType``
    of ``Take Profit Market``/``Take Profit Limit`` or ``Stop Market``/
    ``Stop Limit`` on a trigger order *is* the trader's own level, so it is
    reported as confirmed. A trigger order whose type string we cannot classify
    is reported as estimated with the reason attached. Absence of trigger orders
    is reported as "none detected" — never as "the trader has no TP/SL", since
    exits may be managed manually.

    When several levels exist the nearest one — the one that would fire first —
    is reported, and the note says how many others there are.
    """
    take_profits: list[float] = []
    stops: list[float] = []
    unclassified: list[float] = []

    for order in _flatten(orders):
        if order.coin != coin or not order.is_trigger or not order.trigger_px:
            continue
        kind = order.trigger_kind
        if kind is TriggerKind.TAKE_PROFIT:
            take_profits.append(order.trigger_px)
        elif kind is TriggerKind.STOP_LOSS:
            stops.append(order.trigger_px)
        else:
            unclassified.append(order.trigger_px)

    # Geometry is used only for triggers whose type string is unknown, and the
    # result is labelled estimated.
    inferred: set[float] = set()
    if unclassified and position_side and current_px:
        for price in unclassified:
            above = price > current_px
            is_tp = above if position_side == "LONG" else not above
            (take_profits if is_tp else stops).append(price)
            inferred.add(price)

    def nearest(levels: list[float], is_tp: bool) -> DataPoint:
        if not levels:
            return DataPoint.unavailable("no resting trigger order detected")
        if position_side == "LONG":
            # TP sits above the mark, stop below; nearest fires first.
            chosen = min(levels) if is_tp else max(levels)
        elif position_side == "SHORT":
            chosen = max(levels) if is_tp else min(levels)
        elif current_px:
            chosen = min(levels, key=lambda px: abs(px - current_px))
        else:
            chosen = levels[0]
        if chosen in inferred:
            return DataPoint.estimated(
                chosen, "trigger order type unrecognised; TP/SL inferred from price"
            )
        note = (
            f"nearest of {len(levels)} resting {'TP' if is_tp else 'SL'} trigger orders"
            if len(levels) > 1
            else None
        )
        return DataPoint.confirmed(chosen, note)

    return nearest(take_profits, True), nearest(stops, False)


class WhaleDetector:
    """Builds whale events from Hyperliquid data. Thresholds are applied by
    :class:`~app.whale.filters.WhaleFilter`; ``min_notional`` here is only a
    cheap prefilter so we do not allocate events for retail-sized activity."""

    def __init__(self, price_provider: PriceProvider | None = None) -> None:
        self._price_provider = price_provider or (lambda _coin: None)

    def current_price(self, coin: str) -> float | None:
        try:
            return self._price_provider(coin)
        except Exception:  # a bad price source must not break detection
            return None

    # ── shared enrichment ─────────────────────────────────────
    def _attach_market(self, event: WhaleEvent, reference_px: float | None) -> None:
        current = self.current_price(event.coin)
        event.set("current_px", DataPoint.confirmed(current, "mark price"))
        if current and reference_px:
            event.set(
                "distance_pct",
                DataPoint.estimated(
                    pct_distance(reference_px, current), "computed from mark price"
                ),
            )

    def _attach_position(self, event: WhaleEvent, ctx: PositionContext) -> None:
        position = ctx.position
        if position is None:
            event.set("position_value", DataPoint.unavailable("no open position for this coin"))
            event.set("entry_px", DataPoint.unavailable("no open position for this coin"))
            event.set("liquidation_px", DataPoint.unavailable("no open position for this coin"))
            event.set("leverage", DataPoint.unavailable("no open position for this coin"))
            if not ctx.orders_known:
                note = "trigger orders not fetched"
                event.set("take_profit_px", DataPoint.unavailable(note))
                event.set("stop_loss_px", DataPoint.unavailable(note))
            return

        event.set("position_side", DataPoint.confirmed(position.side))
        event.set("position_size", DataPoint.confirmed(position.abs_size))
        event.set("position_value", DataPoint.confirmed(position.position_value))
        event.set("entry_px", DataPoint.confirmed(position.entry_px))
        # How far the mark has travelled from the entry. Kept separate from
        # ``distance_pct`` (which measures an *order* price against the mark) so
        # the two can never be printed under the same label.
        current_px = self.current_price(event.coin)
        if current_px and position.entry_px:
            event.set(
                "entry_distance_pct",
                DataPoint.estimated(
                    pct_distance(current_px, position.entry_px),
                    "mark price against the position's entry price",
                ),
            )
        event.set(
            "liquidation_px",
            DataPoint.confirmed(position.liquidation_px)
            if position.liquidation_px
            else DataPoint.unavailable("not returned by Hyperliquid for this position"),
        )
        event.set("leverage", DataPoint.confirmed(position.leverage_value))
        event.set("leverage_type", DataPoint.confirmed(position.leverage_type))
        event.set("margin_used", DataPoint.confirmed(position.margin_used))
        event.set("unrealized_pnl", DataPoint.confirmed(position.unrealized_pnl))
        event.set("max_leverage", DataPoint.confirmed(position.max_leverage))
        if ctx.account_value is not None:
            event.set("account_value", DataPoint.confirmed(ctx.account_value))

        if ctx.orders_known:
            take_profit, stop = extract_tpsl(
                ctx.trigger_orders, event.coin, position.side, self.current_price(event.coin)
            )
        else:
            note = "trigger orders not fetched for this wallet"
            take_profit = stop = DataPoint.unavailable(note)
        event.set("take_profit_px", take_profit)
        event.set("stop_loss_px", stop)

        if ctx.first_seen is not None:
            event.set(
                "observed_for",
                DataPoint.estimated(
                    (utc_now() - ctx.first_seen).total_seconds(),
                    "time since this bot first observed the position, not the on-chain open time",
                ),
            )

    # ── 1. large executed trades (market orders) ───────────────
    def from_trade(
        self,
        trade: Trade,
        *,
        wallet: str | None = None,
        context: PositionContext | None = None,
        min_notional: float = 0.0,
    ) -> WhaleEvent | None:
        """A single crossing trade at or above ``min_notional``.

        ``wallet`` selects which participant the event is about; it defaults to
        the taker, whose side is the one Hyperliquid states explicitly.
        """
        notional = trade.notional
        if notional < min_notional:
            return None

        subject = (wallet or trade.taker or trade.buyer or trade.seller)
        ctx = context or PositionContext()
        is_taker = subject is not None and subject == trade.taker

        if is_taker:
            trade_side = trade.taker_side
        elif subject is not None and subject == trade.maker:
            # The maker took the opposite side of the taker's trade.
            trade_side = "SELL" if trade.taker_side == "BUY" else "BUY" if trade.taker_side else None
        else:
            trade_side = trade.taker_side

        position_side = ctx.position.side if ctx.position else None
        event = WhaleEvent(
            event_type=EventType.WHALE_TRADE,
            coin=trade.coin,
            notional=notional,
            value_kind=ValueKind.TRADE_VALUE,
            event_time=trade.time or utc_now(),
            side=position_side or trade_side,
            wallet=subject,
            detection="Large market trade" if is_taker else "Large resting order filled",
            context={
                "tid": trade.tid,
                "hash": trade.hash,
                "source": "ws:trades",
                "role": "taker" if is_taker else "maker",
                "counterparty": trade.maker if is_taker else trade.taker,
            },
        )
        event.set("price", DataPoint.confirmed(trade.px, "execution price"))
        event.set("size", DataPoint.confirmed(trade.sz))
        event.set("trade_side", DataPoint.confirmed(trade_side))
        event.set("taker_side", DataPoint.confirmed(trade.taker_side))
        self._attach_position(event, ctx)
        self._attach_market(event, trade.px)
        return event

    # ── 2. position lifecycle ─────────────────────────────────
    def from_position_change(
        self,
        wallet: str,
        coin: str,
        before: Position | None,
        after: Position | None,
        *,
        context: PositionContext | None = None,
        min_notional: float = 0.0,
    ) -> WhaleEvent | None:
        """Diff two ``clearinghouseState`` snapshots for one (wallet, coin).

        The threshold is applied to the *cash flow* of the change — the USD
        value of the size delta — not to the whole position, so a $1M add to a
        $50M position is judged as $1M.
        """
        size_before = before.szi if before else 0.0
        size_after = after.szi if after else 0.0
        if abs(size_before - size_after) <= DUST:
            return None

        ctx = context or PositionContext()
        if ctx.position is None:
            ctx = replace(ctx, position=after)
        price = (
            self.current_price(coin)
            or (after.entry_px if after else None)
            or (before.entry_px if before else None)
        )
        delta = size_after - size_before
        delta_value = abs(delta) * price if price else None

        flipped = size_before * size_after < 0
        if abs(size_before) <= DUST:
            event_type, detection = EventType.POSITION_OPENED, "Position opened"
        elif abs(size_after) <= DUST:
            event_type, detection = EventType.POSITION_CLOSED, "Position closed"
        elif flipped:
            event_type, detection = EventType.POSITION_FLIPPED, "Position flipped"
        elif abs(size_after) > abs(size_before):
            event_type, detection = EventType.POSITION_INCREASED, "Position increased"
        else:
            event_type, detection = EventType.POSITION_DECREASED, "Position reduced"

        # Closures are measured by the notional that left the book; everything
        # else by the delta's value.
        if event_type is EventType.POSITION_CLOSED:
            notional = (before.notional if before else 0.0) or (delta_value or 0.0)
            value_kind = ValueKind.POSITION_NOTIONAL
        else:
            notional = delta_value or 0.0
            value_kind = ValueKind.POSITION_DELTA

        if notional < min_notional:
            return None

        side = (after.side if after else None) or (before.side if before else None)
        event = WhaleEvent(
            event_type=event_type,
            coin=coin,
            notional=notional,
            value_kind=value_kind,
            event_time=utc_now(),
            side=side,
            wallet=wallet,
            detection=detection,
            context={"source": "rest:clearinghouseState", "delta_size": delta},
        )
        event.set("price", DataPoint.confirmed(price, "mark price at detection"))
        event.set("size", DataPoint.confirmed(abs(size_after)))
        event.set("size_delta", DataPoint.confirmed(delta))
        event.set("size_before", DataPoint.confirmed(abs(size_before)))
        if delta_value is not None:
            event.set(
                "delta_value",
                DataPoint.estimated(delta_value, "|size delta| × mark price"),
            )
        if before is not None:
            event.set("previous_position_value", DataPoint.confirmed(before.position_value))
        if event_type is EventType.POSITION_CLOSED:
            event.set("closed_position_value", DataPoint.confirmed(before.notional if before else None))
            if before is not None and before.unrealized_pnl is not None:
                # The last unrealised PnL we observed before the position went to
                # zero. It is *not* the realised result: the close may have
                # happened at a different price, and fees are not included. It is
                # therefore labelled an estimate, and the renderer says "(est.)".
                event.set(
                    "final_unrealized_pnl",
                    DataPoint.estimated(
                        before.unrealized_pnl,
                        "last observed unrealised PnL before the position closed; "
                        "not the realised result",
                    ),
                )
            if before is None:
                event.set(
                    "historical_position",
                    DataPoint.unavailable("no pre-close snapshot of this position was observed"),
                )

        self._attach_position(event, ctx)
        self._attach_market(event, after.entry_px if after else (before.entry_px if before else None))
        return event

    # ── 3. resting order lifecycle ────────────────────────────
    def from_order_update(
        self,
        wallet: str,
        update: OrderUpdate,
        previous: OrderState | None = None,
        *,
        min_notional: float = 0.0,
    ) -> WhaleEvent | None:
        """Map one ``orderUpdates`` frame to an event.

        ``orderUpdates`` is a per-wallet websocket feed, so it is only available
        for the focus slate (Hyperliquid allows 10 unique addresses across all
        user subscriptions). Wallets outside the slate get the same lifecycle via
        snapshot diffing plus an ``orderStatus`` lookup — see
        :meth:`from_order_disappearance`.
        """
        status = update.status
        reference_notional = update.original_notional or update.notional
        if max(update.notional, reference_notional or 0.0) < min_notional:
            return None

        if status == "open":
            if previous is None:
                event_type, detection = EventType.ORDER_PLACED, "Large limit order placed"
            elif previous.size > update.sz + DUST:
                event_type, detection = EventType.ORDER_PARTIALLY_FILLED, "Large order partially filled"
            elif previous.limit_px != update.limit_px or previous.size != update.sz:
                event_type, detection = EventType.ORDER_MODIFIED, "Large order modified"
            else:
                return None
        elif status == "filled":
            event_type, detection = EventType.ORDER_FILLED, "Large order filled"
        elif is_cancel_status(status):
            event_type, detection = EventType.ORDER_CANCELLED, "Large order cancelled"
        elif is_reject_status(status):
            event_type, detection = EventType.ORDER_REJECTED, "Large order rejected"
        elif status == "triggered":
            event_type, detection = EventType.ORDER_MODIFIED, "Trigger order activated"
        else:
            return None

        notional = reference_notional if event_type is EventType.ORDER_CANCELLED else update.notional
        event = WhaleEvent(
            event_type=event_type,
            coin=update.coin,
            notional=notional or update.notional,
            value_kind=ValueKind.ORDER_NOTIONAL,
            event_time=update.status_timestamp or update.timestamp or utc_now(),
            side=update.direction,
            wallet=wallet,
            detection=detection,
            order_id=update.oid,
            status=status,
            context={"source": "ws:orderUpdates", "raw_status": status},
        )
        self._decorate_order(
            event,
            price=update.limit_px,
            size=update.sz,
            orig_size=update.orig_sz,
            orig_notional=update.original_notional,
            placed_at=update.timestamp,
            status=status,
        )
        return event

    def from_open_order(
        self,
        wallet: str,
        order: OpenOrder,
        previous: OrderState | None = None,
        *,
        min_notional: float = 0.0,
    ) -> WhaleEvent | None:
        """Map a ``frontendOpenOrders`` snapshot entry to a placed/changed event."""
        if order.notional < min_notional:
            return None

        if previous is None:
            event_type, detection = EventType.ORDER_PLACED, "Large resting order detected"
        elif order.sz < previous.size - DUST:
            event_type, detection = EventType.ORDER_PARTIALLY_FILLED, "Large order partially filled"
        elif (order.limit_px != previous.limit_px) or (order.sz > previous.size + DUST):
            event_type, detection = EventType.ORDER_MODIFIED, "Large order modified"
        else:
            return None

        event = WhaleEvent(
            event_type=event_type,
            coin=order.coin,
            notional=order.notional,
            value_kind=ValueKind.ORDER_NOTIONAL,
            event_time=order.timestamp or utc_now(),
            side=order.direction,
            wallet=wallet,
            detection=detection,
            order_id=order.oid,
            status="open",
            context={"source": "rest:frontendOpenOrders"},
        )
        self._decorate_order(
            event,
            price=order.price,
            size=order.sz,
            orig_size=order.orig_sz,
            orig_notional=(order.price * order.orig_sz) if order.price and order.orig_sz else None,
            placed_at=order.timestamp,
            status="open",
            order_type=order.order_type,
            is_trigger=order.is_trigger,
            trigger_px=order.trigger_px,
            reduce_only=order.reduce_only,
            tif=order.tif,
            is_position_tpsl=order.is_position_tpsl,
        )
        return event

    def from_order_disappearance(
        self,
        wallet: str,
        previous: OrderState,
        resolved_status: str | None,
        *,
        min_notional: float = 0.0,
    ) -> WhaleEvent | None:
        """An order that left the book between two snapshots.

        A snapshot diff alone cannot tell a cancellation from a fill, so the
        caller resolves the real outcome with an ``orderStatus`` lookup (weight
        2). If that lookup fails, the event says so instead of guessing.
        """
        if previous.notional < min_notional:
            return None

        if resolved_status == "filled":
            event_type, detection = EventType.ORDER_FILLED, "Large order filled"
        elif is_cancel_status(resolved_status):
            event_type, detection = EventType.ORDER_CANCELLED, "Large order cancelled"
        elif is_reject_status(resolved_status):
            event_type, detection = EventType.ORDER_REJECTED, "Large order rejected"
        elif resolved_status == "triggered":
            event_type, detection = EventType.ORDER_FILLED, "Trigger order activated"
        else:
            event_type = EventType.ORDER_MODIFIED
            detection = "Large order left the book (outcome unresolved)"

        event = WhaleEvent(
            event_type=event_type,
            coin=previous.coin,
            notional=previous.notional,
            value_kind=ValueKind.ORDER_NOTIONAL,
            event_time=utc_now(),
            side=side_label(previous.side) or previous.side,
            wallet=wallet,
            detection=detection,
            order_id=previous.oid,
            status=resolved_status or "unknown",
            context={"source": "rest:frontendOpenOrders+orderStatus"},
        )
        orig_notional = (
            abs(previous.limit_px * previous.orig_size)
            if previous.limit_px and previous.orig_size
            else previous.notional
        )
        self._decorate_order(
            event,
            price=previous.limit_px,
            size=previous.size,
            orig_size=previous.orig_size,
            orig_notional=orig_notional,
            placed_at=previous.placed_at,
            status=resolved_status,
            order_type=previous.order_type,
            is_trigger=previous.is_trigger,
            trigger_px=previous.trigger_px,
            reduce_only=previous.reduce_only,
        )
        if resolved_status is None:
            event.set(
                "status_note",
                DataPoint.unavailable("orderStatus lookup unavailable; cancel vs fill unresolved"),
            )
        return event

    def _decorate_order(
        self,
        event: WhaleEvent,
        *,
        price: float | None,
        size: float | None,
        orig_size: float | None,
        orig_notional: float | None,
        placed_at: datetime | None,
        status: str | None,
        order_type: str | None = None,
        is_trigger: bool = False,
        trigger_px: float | None = None,
        reduce_only: bool = False,
        tif: str | None = None,
        is_position_tpsl: bool = False,
    ) -> None:
        event.set("price", DataPoint.confirmed(price, "order price"))
        event.set("size", DataPoint.confirmed(size))
        event.set("orig_size", DataPoint.confirmed(orig_size))
        if orig_notional:
            event.set("orig_notional", DataPoint.confirmed(orig_notional))
        if orig_size is not None and size is not None:
            filled = max(0.0, orig_size - size)
            event.set("filled_size", DataPoint.confirmed(filled))
            if price:
                event.set("remaining_notional", DataPoint.confirmed(abs(price * size)))
                event.set("filled_notional", DataPoint.confirmed(abs(price * filled)))
        event.set("order_status", DataPoint.confirmed(status_label(status) if status else None))
        event.set("order_type", DataPoint.confirmed(order_type))
        event.set("reduce_only", DataPoint.confirmed(reduce_only))
        event.set("tif", DataPoint.confirmed(tif))
        if is_trigger:
            event.set("trigger_px", DataPoint.confirmed(trigger_px))
            event.set("is_position_tpsl", DataPoint.confirmed(is_position_tpsl))
        if placed_at is not None:
            event.set("placed_at", DataPoint.confirmed(placed_at.isoformat()))
            event.set(
                "resting_for",
                DataPoint.confirmed(
                    max(0.0, (utc_now() - placed_at).total_seconds()),
                    "from the order's own timestamp",
                ),
            )
        self._attach_market(event, price)

    # ── 4. aggregate order-book levels ────────────────────────
    def from_book(
        self, book: L2Book, *, min_notional: float, max_events: int = 4
    ) -> list[WhaleEvent]:
        """Large resting levels from ``l2Book``.

        These carry **no wallet attribution** — Hyperliquid publishes only
        ``px``, ``sz`` and an order count ``n`` per level — and a level may
        aggregate many traders. Events are labelled accordingly.
        """
        current = self.current_price(book.coin)
        events: list[WhaleEvent] = []
        for side, levels in (("BUY", book.bids), ("SELL", book.asks)):
            for level in levels:
                if level.notional < min_notional:
                    continue
                event = WhaleEvent(
                    event_type=EventType.BOOK_LEVEL,
                    coin=book.coin,
                    notional=level.notional,
                    value_kind=ValueKind.BOOK_LEVEL_NOTIONAL,
                    event_time=book.time or utc_now(),
                    side=side,
                    wallet=None,
                    detection="Large aggregate book level",
                    context={"source": "ws:l2Book", "orders_at_level": level.n},
                )
                event.set("price", DataPoint.confirmed(level.px))
                event.set("size", DataPoint.confirmed(level.sz))
                event.set("orders_at_level", DataPoint.confirmed(level.n))
                event.set(
                    "wallet_attribution",
                    DataPoint.unavailable("l2Book is aggregated; Hyperliquid exposes no owner"),
                )
                self._attach_market(event, level.px)
                events.append(event)
                if len(events) >= max_events:
                    return events
        return events
