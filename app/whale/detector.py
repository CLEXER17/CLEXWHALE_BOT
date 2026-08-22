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
from app.hyperliquid.models import Fill, L2Book, OpenOrder, OrderUpdate, Position, Trade
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


#: Words in a fill's ``dir`` string that mean "this fill reduced or removed an
#: existing position", which is the only case where ``dir`` names a side we can
#: report as the side that was liquidated.
_CLOSING_WORDS = ("close", "liquidat", ">")


def _stated_direction(dir_text: str | None) -> str | None:
    """The position side Hyperliquid spells out in a fill's ``dir``, or ``None``.

    ``dir`` is free-form English — ``"Close Long"``, ``"Long > Short"``,
    ``"Open Short"``, ``"Buy"`` — so it is read conservatively:

    * Only a *closing* description counts. ``"Open Long"`` names a side, but the
      side of a position being created, which is no evidence about what was
      force-closed.
    * On a flip such as ``"Long > Short"`` the first side named is the one being
      closed, so the earliest mention wins.
    * ``"Buy"``/``"Sell"`` name an execution direction, not a position side, and
      are deliberately not translated. That is the whole order/position rule.
    """
    if not dir_text:
        return None
    text = dir_text.lower()
    if not any(word in text for word in _CLOSING_WORDS):
        return None
    long_at = text.find("long")
    short_at = text.find("short")
    if long_at < 0 and short_at < 0:
        return None
    if short_at < 0 or (long_at >= 0 and long_at < short_at):
        return "LONG"
    return "SHORT"


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
        # A flat snapshot is not a position. Hyperliquid drops closed positions
        # from ``assetPositions``, but a snapshot taken mid-close can still carry
        # ``szi == 0`` with the entry price, leverage and liquidation price of the
        # position that has just gone — and attaching those would report a
        # position the trader no longer holds (Task F).
        if position is None or position.is_flat:
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

        :attr:`WhaleEvent.side` is the *executed* side — ``BUY`` or ``SELL``. A
        verified position snapshot, when one is attached, is reported separately
        as ``position_side``: an execution is not a position, and a $3M BUY that
        trims a short must not be badged ``SHORT`` in a trade alert.
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

        event = WhaleEvent(
            event_type=EventType.WHALE_TRADE,
            coin=trade.coin,
            notional=notional,
            value_kind=ValueKind.TRADE_VALUE,
            event_time=trade.time or utc_now(),
            side=trade_side,
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

    # ── 1b. verified executions from a wallet's own fills feed ─
    def from_fill(
        self,
        wallet: str,
        fill: Fill,
        *,
        context: PositionContext | None = None,
        min_notional: float = 0.0,
        filled_size: float | None = None,
        order_size: float | None = None,
    ) -> WhaleEvent | None:
        """A verified execution reported on a wallet's ``userEvents`` fills feed.

        This is the second source of ``WHALE_TRADE`` and the one anchored to an
        order: a fill carries ``oid`` (the order that executed) and ``tid`` (the
        execution itself), which is exactly the identity the deduplicator wants.
        The public ``trades`` feed reports the same executions market-wide but
        without an order id, so the two feeds overlap for focus wallets and the
        shared ``("trade", tid, role)`` identity collapses them into one alert.

        Three rules hold here, all of them the order/position separation:

        * A fill *is* an execution. A resting order that has not filled produces
          nothing in this method — orders arrive on ``orderUpdates`` instead.
        * :attr:`WhaleEvent.side` is the executed side, ``BUY`` or ``SELL``, and
          is never translated into ``LONG``/``SHORT``. Hyperliquid's own
          description of what the fill did to the position (``dir``) is reported
          verbatim as a separate, clearly-labelled field.
        * The threshold is applied to *this* execution's notional. Partial fills
          are separate executions and are never summed into one alert, because
          summing them would announce a trade of a size that never happened. The
          cumulative progress of the order is reported alongside instead.
        """
        if fill.is_liquidation:
            # Forced closes are not discretionary trades; from_liquidation owns
            # them, and routing one here would announce it as a whale's decision.
            return None
        notional = fill.notional
        if notional < min_notional:
            return None

        ctx = context or PositionContext()
        trade_side = side_label(fill.side)
        crossed = bool(fill.crossed)
        event = WhaleEvent(
            event_type=EventType.WHALE_TRADE,
            coin=fill.coin,
            notional=notional,
            value_kind=ValueKind.TRADE_VALUE,
            event_time=fill.time or utc_now(),
            side=trade_side,
            wallet=(wallet or "").lower() or None,
            detection="Large market trade" if crossed else "Large resting order filled",
            order_id=fill.oid,
            context={
                "tid": fill.tid,
                "hash": fill.hash,
                "oid": fill.oid,
                "source": "ws:userEvents:fills",
                "role": "taker" if crossed else "maker",
                "dir": fill.dir,
            },
        )
        event.set("price", DataPoint.confirmed(fill.px, "execution price"))
        event.set("size", DataPoint.confirmed(abs(fill.sz)))
        event.set("trade_side", DataPoint.confirmed(trade_side))
        event.set(
            "execution_dir",
            DataPoint.confirmed(str(fill.dir), "direction Hyperliquid reported for this fill")
            if fill.dir
            else DataPoint.unavailable("not reported on this fill"),
        )
        event.set(
            "fee",
            DataPoint.confirmed(fill.fee)
            if fill.fee is not None
            else DataPoint.unavailable("not reported on this fill"),
        )
        # ``closedPnl`` is this wallet's realised result on this fill. It is 0 for
        # a fill that only opens or adds, so it is reported only when the exchange
        # actually settled something.
        if fill.closed_pnl is not None and abs(fill.closed_pnl) > 0:
            event.set(
                "realized_pnl",
                DataPoint.confirmed(fill.closed_pnl, "closedPnl on this fill"),
            )
        if filled_size is not None:
            event.set(
                "order_filled_size",
                DataPoint.confirmed(abs(filled_size), "cumulative verified fills of this order"),
            )
            if order_size and abs(order_size) > DUST:
                event.set(
                    "order_fill_pct",
                    DataPoint.confirmed(
                        100.0 * abs(filled_size) / abs(order_size),
                        "share of the original order size that has executed",
                    ),
                )
        self._attach_position(event, ctx)
        self._attach_market(event, fill.px)
        return event

    # ── 2. forced liquidation ─────────────────────────────────
    def from_liquidation(
        self,
        subscribed_wallet: str,
        fill: Fill,
        *,
        context: PositionContext | None = None,
        min_notional: float = 0.0,
    ) -> WhaleEvent | None:
        """A liquidation the exchange reported on a per-wallet fills feed.

        ``fill.liquidation`` is the only public evidence that a liquidation
        happened at all: there is no global liquidations feed, so this covers the
        focus slate only (`.agent/API_NOTES.md` §5).

        Two things are easy to get wrong here and are handled explicitly.

        **Who was liquidated.** A fill on ``subscribed_wallet``'s feed carrying a
        ``liquidation`` object does *not* mean that wallet was the one liquidated
        — it may have been on the other side of someone else's forced close.
        Hyperliquid names the liquidated party in ``liquidation.liquidatedUser``,
        so when that field is present it *is* the subject, and the subscribed
        wallet becomes the counterparty. Position context is attached only when
        the two are the same wallet; otherwise we hold no snapshot of the party
        this event is about, and inventing one is not an option.

        **Which side was closed.** Never inferred from the fill's buy/sell
        direction: a BUY closes a short and also opens a long, and the point of
        the order/position separation is that an execution side is not a position
        side. Only three sources count, in order — the last verified
        ``clearinghouseState`` snapshot, the sign of ``startPosition`` (the feed's
        own report of the position size before the fill), and ``dir`` when
        Hyperliquid spells the direction out. With none of them the side is left
        unavailable and the alert prints no side badge.
        """
        notional = fill.notional
        if notional < min_notional:
            return None
        detail = fill.liquidation or {}
        if not isinstance(detail, dict):
            detail = {}

        liquidated_user = detail.get("liquidatedUser")
        subject = (
            str(liquidated_user).lower()
            if isinstance(liquidated_user, str) and liquidated_user
            else subscribed_wallet
        )
        own = subject.lower() == (subscribed_wallet or "").lower()
        ctx = (context or PositionContext()) if own else PositionContext()

        side, side_source = self._liquidated_side(ctx, fill)
        event = WhaleEvent(
            event_type=EventType.WHALE_LIQUIDATED,
            coin=fill.coin,
            notional=notional,
            value_kind=ValueKind.LIQUIDATION_VALUE,
            event_time=fill.time or utc_now(),
            side=side,
            wallet=subject,
            detection="Forced liquidation",
            context={
                "tid": fill.tid,
                "hash": fill.hash,
                "oid": fill.oid,
                "source": "ws:userEvents",
                "subscribed_wallet": subscribed_wallet,
                # Set only when the subscribed wallet was *not* the liquidated
                # party, so an audit can tell the two roles apart later.
                "counterparty": None if own else subscribed_wallet,
            },
        )
        event.set("price", DataPoint.confirmed(fill.px, "liquidation fill price"))
        event.set("size", DataPoint.confirmed(abs(fill.sz)))
        # The fill's own direction, kept as an order-side word so nothing
        # downstream can mistake it for the position side.
        event.set("trade_side", DataPoint.confirmed(side_label(fill.side)))
        if side is not None:
            event.set("liquidated_side", DataPoint.confirmed(side, side_source))
        else:
            event.set(
                "liquidated_side",
                DataPoint.unavailable("no verified position side for this liquidation"),
            )

        mark_px = detail.get("markPx")
        event.set(
            "liquidation_mark_px",
            DataPoint.confirmed(float(mark_px), "mark price at liquidation")
            if isinstance(mark_px, (int, float))
            else DataPoint.unavailable("not reported on this liquidation"),
        )
        method = detail.get("method")
        event.set(
            "liquidation_method",
            DataPoint.confirmed(str(method))
            if isinstance(method, str) and method
            else DataPoint.unavailable("not reported on this liquidation"),
        )

        # ``closedPnl`` is the subscribed wallet's realised result on *its* fill.
        # It is only the liquidated party's loss when those are the same wallet.
        if own and fill.closed_pnl is not None:
            event.set(
                "realized_pnl",
                DataPoint.confirmed(fill.closed_pnl, "closedPnl on the liquidation fill"),
            )
        else:
            event.set(
                "realized_pnl",
                DataPoint.unavailable(
                    "closedPnl belongs to the counterparty, not the liquidated wallet"
                    if not own
                    else "not reported on this fill"
                ),
            )

        self._attach_market(event, fill.px)
        return event

    @staticmethod
    def _liquidated_side(ctx: PositionContext, fill: Fill) -> tuple[str | None, str]:
        """The side of the position that was closed, and where that came from."""
        if ctx.position is not None and ctx.position.side in {"LONG", "SHORT"}:
            return ctx.position.side, "last verified position snapshot"
        start = fill.start_position
        if start is not None and abs(start) > DUST:
            side = "LONG" if start > 0 else "SHORT"
            return side, "sign of startPosition on the liquidation fill"
        stated = _stated_direction(fill.dir)
        if stated is not None:
            return stated, "direction stated by Hyperliquid on the fill"
        return None, ""

    # ── 3. position lifecycle ─────────────────────────────────
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

        if ctx.position is None and after is not None and abs(after.szi) > DUST:
            # Only a snapshot that still holds a position may act as the live
            # context. A closing snapshot carries ``szi == 0`` with stale entry,
            # leverage and liquidation figures attached; presenting those as the
            # trader's current position would be reporting a position that no longer
            # exists. The close's own figures are read from ``before`` below.
            ctx = replace(ctx, position=after)

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

        # The side comes from the last snapshot that actually held a position, and
        # from nothing else (Task F). A closing snapshot has ``szi == 0`` and so no
        # side of its own, so reading it there would relabel every closed LONG as a
        # SHORT; and an execution's BUY/SELL is never evidence either way, because a
        # SELL may be reducing a long and a BUY may be reducing a short.
        side = None
        for snapshot in (after, before):
            if snapshot is not None and abs(snapshot.szi) > DUST:
                side = snapshot.side
                break
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
        # Same rule for the market comparison: a closed position's entry price is
        # not a reference the reader can act on, so nothing is compared against it.
        reference_entry = (
            ctx.position.entry_px
            if ctx.position is not None and not ctx.position.is_flat
            else None
        )
        self._attach_market(event, reference_entry)
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
        if event_type is EventType.ORDER_FILLED:
            # What executed is what was still resting a moment ago. With no
            # previous state the original size is the best figure the feed offers.
            self._as_execution(
                event,
                executed_size=previous.size if previous is not None else update.orig_sz,
                price=update.limit_px,
                fallback_notional=update.original_notional or update.notional,
            )
        elif event_type is EventType.ORDER_PARTIALLY_FILLED and previous is not None:
            self._as_execution(
                event,
                executed_size=max(0.0, previous.size - update.sz),
                price=update.limit_px,
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
        if event_type is EventType.ORDER_PARTIALLY_FILLED and previous is not None:
            self._as_execution(
                event,
                executed_size=max(0.0, previous.size - order.sz),
                price=order.price,
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
        if event_type is EventType.ORDER_FILLED:
            # The order vanished and ``orderStatus`` said it filled, so the size
            # that was still resting is the size that executed.
            self._as_execution(
                event,
                executed_size=previous.size,
                price=previous.limit_px,
                fallback_notional=previous.notional,
            )
        return event

    def _as_execution(
        self,
        event: WhaleEvent,
        *,
        executed_size: float | None,
        price: float | None,
        fallback_notional: float | None = None,
    ) -> None:
        """Re-measure a fill as the execution it is.

        An order event's natural figure is the order's notional — for a *filled*
        order that is the wrong number twice over. Hyperliquid reports a completed
        order with its remaining size, which is zero, so the raw figure is ``$0``;
        and the order's face value is what the trader *asked* for, which is not
        what executed once part of it had already been consumed.

        So the figure becomes the value that actually changed hands, and
        :class:`ValueKind` becomes ``TRADE_VALUE`` — which is also what routes the
        event to the *trade* threshold rather than the resting-order one. The
        executed size and value are kept as their own data points so the renderer
        never has to reach for the remaining-size field.
        """
        executed: float | None = None
        if executed_size is not None and price:
            executed = abs(float(price) * float(executed_size))
        if not executed:
            executed = abs(float(fallback_notional or 0.0)) or event.notional
        event.notional = executed
        event.value_kind = ValueKind.TRADE_VALUE
        if executed_size is not None:
            event.set("executed_size", DataPoint.confirmed(abs(float(executed_size))))
        event.set("executed_notional", DataPoint.confirmed(executed))

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
