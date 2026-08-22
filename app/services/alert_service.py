"""Alert rendering and delivery.

Two responsibilities, kept apart so each is testable on its own:

**Rendering** (:meth:`AlertService.render`) turns a :class:`WhaleEvent` into the
Telegram message. Every optional field is read through its
:class:`~app.utils.formatting.DataPoint`, so:

* a value Hyperliquid did not give us prints as ``N/A`` — never as a number;
* a value *we* computed prints with an ``(est.)`` marker, so a derived figure can
  never be mistaken for a level the trader actually set;
* take-profit / stop-loss come only from resting trigger orders in
  ``frontendOpenOrders``. If none were found the line says ``N/A``; if we never
  had the budget to look, it says ``N/A (not checked)``. Neither is ever a guess.

**Delivery** is a single serialised sender task draining a bounded queue, so a
burst of detections cannot fan out into a burst of Telegram calls. Administrators
receive alerts unless they sent ``/stop``; ordinary subscribers receive them only
while public mode is on. Follow-up alerts for a wallet+coin are sent as Telegram
replies to the first alert of that thread, so a chat reads as one conversation
per position. Each chat has its own token bucket, every send is written to
``alert_history``, and a user who has blocked the bot is marked so we stop
trying.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.config import Settings
from app.database.base import Database
from app.database.repository import AlertRepository, EventRepository, UserRepository
from app.services.admin_service import AdminService
from app.services.settings_service import SettingsService
from app.utils.formatting import (
    DIVIDER,
    Confidence,
    DataPoint,
    escape_html,
    fmt_duration,
    fmt_leverage,
    fmt_pct,
    fmt_price,
    fmt_size,
    fmt_time,
    fmt_usd,
    fmt_usd_full,
    utc_now,
)
from app.utils.logging import get_logger
from app.utils.ratelimit import TokenBucket
from app.whale.events import EventType, ValueKind, WhaleEvent
from app.whale.lifecycle import ORDER_SIDES

log = get_logger(__name__)

QUEUE_MAX = 500
#: Pause between consecutive chats so a broadcast does not burst Telegram.
INTER_CHAT_DELAY = 0.06
RECIPIENT_CACHE_TTL = 30.0
FOOTER = "🐋 CLEXER WHALE MONITOR"

#: How long a reply thread stays open. A wallet that goes quiet for longer starts
#: a fresh thread rather than replying to a message far up the chat history.
THREAD_TTL = timedelta(hours=12)
#: Upper bound on the in-memory thread map, so a busy day cannot grow it without
#: limit. Oldest entries are discarded first.
THREAD_MAX = 2000

#: Header per event type. The first word of an alert has to be true on its own,
#: because it is what a reader sees in a notification preview: only something that
#: actually executed may be headed ``WHALE TRADE``, and an order that is merely
#: resting, changed or pulled says so.
HEADERS: dict[EventType, str] = {
    EventType.WHALE_TRADE: "🐋 WHALE TRADE",
    EventType.WHALE_LIQUIDATED: "💥 WHALE LIQUIDATED",
    EventType.POSITION_OPENED: "🐋 WHALE POSITION OPENED",
    EventType.POSITION_INCREASED: "🐋 WHALE POSITION INCREASED",
    EventType.POSITION_DECREASED: "🐋 WHALE POSITION REDUCED",
    EventType.POSITION_CLOSED: "🐋 WHALE POSITION CLOSED",
    EventType.POSITION_FLIPPED: "🔄 WHALE POSITION FLIPPED",
    EventType.ORDER_PLACED: "🐋 LARGE LIMIT ORDER",
    EventType.ORDER_MODIFIED: "✏️ WHALE ORDER MODIFIED",
    EventType.ORDER_PARTIALLY_FILLED: "🐋 WHALE TRADE — PARTIAL FILL",
    EventType.ORDER_FILLED: "🐋 WHALE TRADE — ORDER FILLED",
    EventType.ORDER_CANCELLED: "🚨 WHALE ORDER CANCELLED",
    EventType.ORDER_REJECTED: "⛔ WHALE ORDER REJECTED",
    EventType.BOOK_LEVEL: "🐋 LARGE BOOK LEVEL",
}

#: Position openings and increases lead with the direction, so a reader can tell
#: a new long from a new short before reading a single field.
DIRECTIONAL_HEADERS: dict[tuple[EventType, str], str] = {
    (EventType.POSITION_OPENED, "LONG"): "📈 WHALE LONG POSITION OPENED",
    (EventType.POSITION_OPENED, "SHORT"): "📉 WHALE SHORT POSITION OPENED",
    (EventType.POSITION_INCREASED, "LONG"): "📈 WHALE LONG POSITION INCREASED",
    (EventType.POSITION_INCREASED, "SHORT"): "📉 WHALE SHORT POSITION INCREASED",
}

#: The one line that says what kind of evidence the alert rests on. An execution
#: is labelled ``VERIFIED EXECUTION``; an intention is labelled as *not* an
#: execution, in those words, so a $6.83M order that was merely cancelled can
#: never be read as a $6.83M trade.
VERIFICATION_LABELS: dict[EventType, str] = {
    EventType.WHALE_TRADE: "VERIFIED EXECUTION",
    EventType.WHALE_LIQUIDATED: "VERIFIED LIQUIDATION",
    EventType.ORDER_FILLED: "VERIFIED EXECUTION",
    EventType.ORDER_PARTIALLY_FILLED: "VERIFIED PARTIAL EXECUTION",
    EventType.POSITION_OPENED: "VERIFIED POSITION",
    EventType.POSITION_INCREASED: "VERIFIED POSITION CHANGE",
    EventType.POSITION_DECREASED: "VERIFIED POSITION CHANGE",
    EventType.POSITION_FLIPPED: "VERIFIED POSITION CHANGE",
    EventType.POSITION_CLOSED: "VERIFIED POSITION CLOSURE",
    EventType.ORDER_PLACED: "RESTING ORDER — NOT AN EXECUTION",
    EventType.ORDER_MODIFIED: "ORDER CHANGED — NOT AN EXECUTION",
    EventType.ORDER_CANCELLED: "ORDER CANCELLED — NOTHING WAS TRADED",
    EventType.ORDER_REJECTED: "ORDER REJECTED — NOTHING WAS TRADED",
    EventType.BOOK_LEVEL: "AGGREGATE BOOK LEVEL — NOT AN EXECUTION",
}

SIDE_BADGES = {
    "LONG": "📈 LONG",
    "SHORT": "📉 SHORT",
    "BUY": "🟢 BUY",
    "SELL": "🔴 SELL",
}


def marker(point: DataPoint) -> str:
    """``(est.)`` for anything we derived rather than read from the API."""
    return " (est.)" if point.confidence is Confidence.ESTIMATED else ""


def _short_reason(note: str | None) -> str:
    if not note:
        return ""
    text = note.strip()
    return text if len(text) <= 60 else text[:57] + "..."


def _distance_text(pct: float | None) -> str:
    """``+1.37% above`` / ``-0.36% below`` — never a bare sign.

    An order price sitting above or below the mark means opposite things for a
    buyer and a seller, so the word is always spelled out.
    """
    if pct is None:
        return "N/A"
    if pct > 0:
        return f"{fmt_pct(pct)} above"
    if pct < 0:
        return f"{fmt_pct(pct)} below"
    return "at the mark price"


def _is_missing_reply(exc: BadRequest) -> bool:
    """True when Telegram refused a reply because the target message is gone."""
    text = str(exc).lower()
    return "reply" in text and ("not found" in text or "message to be replied" in text)


def thread_key_for(event: WhaleEvent) -> str | None:
    """Which conversation an alert belongs to.

    One thread per wallet **and** coin: a whale running BTC and ETH positions
    produces two readable threads instead of one interleaved one. Book-level
    events carry no wallet (Hyperliquid aggregates the book anonymously), so they
    are never threaded — pretending otherwise would imply an attribution that
    does not exist.
    """
    if not event.wallet:
        return None
    return f"{event.wallet.lower()}:{event.coin.upper()}"[:96]


@dataclass(slots=True)
class _AlertJob:
    event_type: str
    coin: str
    dedup_key: str
    text: str
    event_id: int | None = None
    thread_key: str | None = None
    queued_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class _Recipient:
    chat_id: int
    telegram_id: int
    is_admin: bool


class AlertService:
    """Renders whale events and broadcasts them to the right chats."""

    def __init__(
        self,
        env: Settings,
        database: Database,
        settings: SettingsService,
        admins: AdminService,
        bot: Bot | None = None,
    ) -> None:
        self.env = env
        self.db = database
        self.settings = settings
        self.admins = admins
        self.bot = bot

        self._queue: asyncio.Queue[_AlertJob] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._sender: asyncio.Task[None] | None = None
        self._running = False

        rate = max(1, int(env.alert_rate_per_minute))
        self._buckets = TokenBucket(rate=rate / 60.0, capacity=min(float(rate), 12.0))
        self._recipients: list[_Recipient] = []
        self._recipients_at: datetime | None = None

        #: ``(chat_id, thread_key) -> (message_id, last_used)``. The root message
        #: every follow-up alert for that wallet/coin replies to.
        self._threads: dict[tuple[int, str], tuple[int, datetime]] = {}

        # counters surfaced through /status and /health
        self.queued = 0
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self.throttled = 0
        self.blocked = 0
        #: Alerts discarded because no recipient could be resolved. Counted, not
        #: swallowed: with 400 whale events and "Alerts delivered: 0" this is the
        #: number that says whether the pipeline stopped at delivery.
        self.no_recipients = 0
        #: Alerts not enqueued because the bot was globally paused (/pause).
        self.paused_drops = 0
        self.last_sent_at: datetime | None = None
        self._no_recipient_warned_at: datetime | None = None

    # ── lifecycle ─────────────────────────────────────────────
    def attach_bot(self, bot: Bot) -> None:
        """Wire in the Telegram bot once the application has been built."""
        self.bot = bot

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._warm_threads()
        self._sender = asyncio.create_task(self._sender_loop(), name="alert-sender")
        log.info("Alert service started", extra={"rate_per_minute": self.env.alert_rate_per_minute})

    async def stop(self) -> None:
        self._running = False
        if self._sender is not None:
            self._sender.cancel()
            try:
                await self._sender
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
            self._sender = None
        log.info("Alert service stopped", extra={"sent": self.sent, "dropped": self.dropped})

    # ── engine entry point ────────────────────────────────────
    async def enqueue(self, event: WhaleEvent, event_id: int | None = None) -> None:
        """``WhaleEngine.alert_callback``. Renders now, sends from the queue.

        Rendering happens on the caller's side so the message reflects the data
        as observed, and so a slow Telegram API can never back-pressure the
        detection pipeline.
        """
        if self.settings.config.paused:
            # Global pause: nothing leaves the bot. Counted so /status can say
            # how much was withheld rather than looking like a silent failure.
            self.paused_drops += 1
            return

        try:
            text = self.render(event)
        except Exception:
            log.exception("Alert rendering failed", extra={"event": event.event_type.value})
            return

        job = _AlertJob(
            event_type=event.event_type.value,
            coin=event.coin,
            dedup_key=event.dedup_key,
            text=text,
            event_id=event_id,
            thread_key=thread_key_for(event),
        )
        try:
            self._queue.put_nowait(job)
            self.queued += 1
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning(
                "Alert queue full; alert dropped",
                extra={"dropped_total": self.dropped, "event": job.event_type},
            )

    # ── rendering ─────────────────────────────────────────────
    def render(self, event: WhaleEvent) -> str:
        if event.event_type is EventType.BOOK_LEVEL:
            return self._render_book(event)
        if event.event_type is EventType.WHALE_LIQUIDATED:
            return self._render_liquidation(event)
        if event.is_execution:
            # WHALE_TRADE and the two fill events. A fill is an execution, so it
            # gets the execution format — never the resting-order one.
            return self._render_trade(event)
        if event.is_order_event:
            return self._render_order(event)
        return self._render_position(event)

    # -- verified executions (spec §4) --
    def _render_trade(self, event: WhaleEvent) -> str:
        """The primary whale feed: a trade that actually happened.

        Every line here describes the *execution* — the price it printed at, the
        quantity that moved, the USD that changed hands. Position fields follow
        only when a verified ``clearinghouseState`` snapshot backs them, and they
        are labelled as position fields, because the size a wallet now holds and
        the size it just traded are different numbers.
        """
        coin = escape_html(event.coin)
        lines = [
            self._header(event),
            DIVIDER,
            f"🪙 <b>{coin}</b>",
            self._side_line(event),
        ]

        price = event.point("price")
        lines.append(
            f"💵 <b>Price:</b> {fmt_price(price.value)}{marker(price)}"
            if price.available
            else "💵 <b>Price:</b> N/A"
        )

        # The quantity that executed. For a fill this is what was consumed, which
        # is not the remaining size the exchange reports once the order is done.
        quantity = event.point("executed_size")
        if not quantity.available:
            quantity = event.point("size")
        if quantity.available:
            lines.append(f"📦 <b>Quantity:</b> {fmt_size(quantity.value)} {coin}")

        lines.append(f"💰 <b>Executed:</b> {fmt_usd_full(event.notional)}")

        if event.order_id is not None:
            # A fill: name the order it belonged to, and what remains of it.
            lines.append(f"🧾 <b>Order:</b> <code>{event.order_id}</code>")
            original = event.point("orig_notional")
            if original.available:
                lines.append(
                    f"📄 <b>Order value:</b> {fmt_usd(original.value)}{marker(original)}"
                )
            remaining = event.point("remaining_notional")
            if remaining.available and float(remaining.value) > 0:
                lines.append(f"📉 <b>Still resting:</b> {fmt_usd(remaining.value)}{marker(remaining)}")

        lines.extend(self._position_context_lines(event))

        current = event.point("current_px")
        if current.available:
            lines.append(f"📊 <b>Current:</b> {fmt_price(current.value)}")
        entry_distance = event.point("entry_distance_pct")
        if entry_distance.available:
            lines.append(f"📐 <b>From Entry:</b> {fmt_pct(entry_distance.value)}")
        elif event.has("distance_pct"):
            lines.append(
                f"📐 <b>Fill vs mark:</b> {_distance_text(event.numeric('distance_pct'))}"
            )

        lines.append(DIVIDER)
        lines.extend(self._trader_lines(event))
        lines.append(self._time_line(event))
        lines.append(self._verification_line(event))
        lines.append(self._route_line(event))
        lines.append(DIVIDER)
        lines.append(FOOTER)
        return "\n".join(lines)

    def _position_context_lines(self, event: WhaleEvent) -> list[str]:
        """What the wallet holds, if a verified snapshot said so.

        Kept separate from the execution lines above and prefixed as *Position*
        throughout: an execution and the position it belongs to are two objects,
        and a reader must never have to guess which number is which.
        """
        position_value = event.point("position_value")
        if not position_value.available:
            reason = _short_reason(position_value.note) or "wallet not enriched yet"
            return [f"ℹ️ <b>Position data:</b> unavailable — {escape_html(reason)}"]

        lines = [
            f"💼 <b>Position:</b> {fmt_usd_full(position_value.value)}{marker(position_value)}"
        ]
        held = event.point("position_size")
        if held.available:
            lines.append(f"📦 <b>Position size:</b> {fmt_size(held.value)} {escape_html(event.coin)}")

        entry = event.point("entry_px")
        lines.append(
            f"🎯 <b>Entry:</b> {fmt_price(entry.value)}{marker(entry)}"
            if entry.available
            else "🎯 <b>Entry:</b> N/A"
        )

        leverage = event.point("leverage")
        lines.append(
            f"⚡ <b>Leverage:</b> "
            f"{fmt_leverage(leverage.value, event.value('leverage_type'))}{marker(leverage)}"
            if leverage.available
            else "⚡ <b>Leverage:</b> N/A"
        )

        liq = event.point("liquidation_px")
        lines.append(
            f"💀 <b>Liquidation:</b> {fmt_price(liq.value)}{marker(liq)}"
            if liq.available
            else "💀 <b>Liquidation:</b> N/A"
        )

        lines.append(self._tpsl_line(event, "take_profit_px", "🎯", "TP"))
        lines.append(self._tpsl_line(event, "stop_loss_px", "🛑", "SL"))

        pnl = event.point("unrealized_pnl")
        if pnl.available:
            emoji = "🟢" if float(pnl.value) >= 0 else "🔴"
            lines.append(f"{emoji} <b>Unrealised:</b> {fmt_usd(pnl.value)}{marker(pnl)}")

        margin = event.point("margin_used")
        if margin.available:
            lines.append(f"🏦 <b>Margin:</b> {fmt_usd(margin.value)}{marker(margin)}")
        return lines

    # -- forced liquidations --
    def _render_liquidation(self, event: WhaleEvent) -> str:
        """A position the exchange closed, rendered from the fill only.

        Everything printed here comes from the liquidation fill itself. The
        position that was liquidated no longer exists, so entry price, leverage,
        margin and TP/SL are *not* reconstructed from the last snapshot we
        happened to hold — one line says they are not reported instead
        (`.agent/API_NOTES.md` §5). The side is the exception, and only because
        it is taken from a verified source that is named in the alert.
        """
        coin = escape_html(event.coin)
        lines = [
            self._header(event),
            DIVIDER,
            f"🪙 <b>{coin}</b>",
            self._liquidated_side_line(event),
            f"💥 <b>Liquidated value:</b> {fmt_usd_full(event.notional)}",
        ]

        price = event.point("price")
        lines.append(
            f"💵 <b>Fill price:</b> {fmt_price(price.value)}{marker(price)}"
            if price.available
            else "💵 <b>Fill price:</b> N/A"
        )

        size = event.point("size")
        if size.available:
            lines.append(f"📦 <b>Size:</b> {fmt_size(size.value)} {coin}")

        mark = event.point("liquidation_mark_px")
        if mark.available:
            lines.append(f"📊 <b>Mark at liquidation:</b> {fmt_price(mark.value)}{marker(mark)}")

        method = event.point("liquidation_method")
        if method.available:
            lines.append(f"🏷 <b>Method:</b> {escape_html(str(method.value))}")

        # The execution direction, labelled as such. A BUY here closed a short;
        # it is not itself a position side, so the two never share a line.
        trade_side = event.point("trade_side")
        if trade_side.available:
            lines.append(f"🔀 <b>Fill direction:</b> {escape_html(str(trade_side.value))}")

        realized = event.point("realized_pnl")
        if realized.available:
            emoji = "🟢" if float(realized.value) >= 0 else "🔴"
            lines.append(
                f"{emoji} <b>Realized PnL:</b> {fmt_usd(realized.value)}{marker(realized)}"
            )
        else:
            reason = _short_reason(realized.note)
            lines.append(
                f"⚪ <b>Realized PnL:</b> N/A — {escape_html(reason)}"
                if reason
                else "⚪ <b>Realized PnL:</b> N/A"
            )

        # Hyperliquid reports no leverage or margin figure for the moment of a
        # liquidation on any public feed, and the pre-liquidation snapshot is not
        # that moment. Saying so beats printing a number from seconds earlier.
        lines.append("ℹ️ Leverage and margin at liquidation are not reported by Hyperliquid")

        counterparty = event.context.get("counterparty")
        if counterparty:
            # The subscribed wallet was on the other side, not the liquidated one.
            lines.append(
                "🔗 <b>Seen via:</b> <code>"
                f"{escape_html(str(counterparty).lower())}</code> (counterparty fill)"
            )

        lines.append(DIVIDER)
        lines.extend(self._trader_lines(event))
        lines.append(self._time_line(event))
        lines.append(self._verification_line(event))
        lines.append(self._route_line(event))
        lines.append(DIVIDER)
        lines.append(FOOTER)
        return "\n".join(lines)

    # -- position / trade signals (spec §16) --
    def _render_position(self, event: WhaleEvent) -> str:
        coin = escape_html(event.coin)
        lines = [
            self._header(event),
            DIVIDER,
            f"🪙 <b>{coin}</b>",
            self._side_line(event),
        ]

        value_line = self._value_line(event)
        if value_line:
            lines.append(value_line)

        position_value = event.point("position_value")
        entry = event.point("entry_px")
        # Position fields are only meaningful once the wallet has been enriched
        # from ``clearinghouseState``. While there is a live position we print
        # "N/A" for anything Hyperliquid withheld; with no position context at
        # all we print one line saying why, instead of a wall of N/A.
        live_position = position_value.available
        has_context = live_position or entry.available

        if live_position:
            lines.append(
                f"💼 <b>Position:</b> {fmt_usd_full(position_value.value)}{marker(position_value)}"
            )

        if entry.available:
            lines.append(f"🎯 <b>Entry:</b> {fmt_price(entry.value)}{marker(entry)}")
        elif live_position:
            lines.append("🎯 <b>Entry:</b> N/A")

        if has_context:
            leverage = event.point("leverage")
            if leverage.available:
                lines.append(
                    f"⚡ <b>Leverage:</b> "
                    f"{fmt_leverage(leverage.value, event.value('leverage_type'))}"
                    f"{marker(leverage)}"
                )
            elif live_position:
                lines.append("⚡ <b>Leverage:</b> N/A")

            liq = event.point("liquidation_px")
            if liq.available:
                lines.append(f"💀 <b>Liquidation:</b> {fmt_price(liq.value)}{marker(liq)}")
            elif live_position:
                # Hyperliquid omits liquidationPx for some cross positions.
                lines.append("💀 <b>Liquidation:</b> N/A")

        current = event.point("current_px")
        if current.available:
            lines.append(f"📊 <b>Current:</b> {fmt_price(current.value)}")
        # A *position* distance is measured against its entry, and is labelled
        # "From Entry" so it can never be read as an order's distance from the
        # mark. (An execution's own distance is rendered by ``_render_trade``;
        # only position events reach this method.)
        entry_distance = event.point("entry_distance_pct")
        if entry_distance.available:
            lines.append(f"📐 <b>From Entry:</b> {fmt_pct(entry_distance.value)}")

        if live_position or event.has("take_profit_px") or event.has("stop_loss_px"):
            lines.append(self._tpsl_line(event, "take_profit_px", "🎯", "TP"))
            lines.append(self._tpsl_line(event, "stop_loss_px", "🛑", "SL"))

        size = event.point("position_size")
        if not size.available:
            size = event.point("size")
        if size.available:
            # Only position events reach this method, so the figure is the size
            # the wallet holds — labelled as such so it is never read as the
            # quantity that just traded.
            lines.append(f"📦 <b>Position size:</b> {fmt_size(size.value)} {coin}")

        if event.event_type is EventType.POSITION_CLOSED:
            lines.append(self._closed_pnl_line(event))
        else:
            pnl = event.point("unrealized_pnl")
            if pnl.available:
                emoji = "🟢" if float(pnl.value) >= 0 else "🔴"
                lines.append(f"{emoji} <b>Unrealised:</b> {fmt_usd(pnl.value)}{marker(pnl)}")

        margin = event.point("margin_used")
        if margin.available:
            lines.append(f"🏦 <b>Margin:</b> {fmt_usd(margin.value)}{marker(margin)}")

        observed = event.point("observed_for")
        if observed.available:
            # Observation window, not an on-chain holding time (spec §6/§34).
            lines.append(
                f"👁 <b>Observed:</b> {fmt_duration(float(observed.value))}"
                " (since first seen by this monitor)"
            )

        if not has_context:
            if event.event_type is EventType.POSITION_CLOSED:
                # Entry, leverage, liquidation and TP/SL belong to a position
                # that no longer exists. We do not reconstruct them, and we do
                # not print a wall of N/A pretending we tried.
                lines.append("ℹ️ Historical position details unavailable")
            else:
                reason = _short_reason(position_value.note) or "wallet not enriched yet"
                lines.append(f"ℹ️ <b>Position data:</b> unavailable — {escape_html(reason)}")

        lines.append(DIVIDER)
        lines.extend(self._trader_lines(event))
        lines.append(self._time_line(event))
        lines.append(self._verification_line(event))
        lines.append(self._route_line(event))
        lines.append(DIVIDER)
        lines.append(FOOTER)
        return "\n".join(lines)

    # -- resting order alerts (spec §17 / §18) --
    def _render_order(self, event: WhaleEvent) -> str:
        coin = escape_html(event.coin)
        cancelled = event.event_type is EventType.ORDER_CANCELLED
        lines = [
            self._header(event),
            DIVIDER,
            f"🪙 <b>{coin}</b>",
            self._order_side_line(event),
        ]

        price = event.point("price")
        lines.append(
            f"💵 <b>Price:</b> {fmt_price(price.value)}{marker(price)}"
            if price.available
            else "💵 <b>Price:</b> N/A"
        )

        if cancelled:
            original = event.point("orig_notional")
            remaining = event.point("remaining_notional")
            lines.append(
                f"💰 <b>Original Value:</b> "
                f"{fmt_usd(original.value if original.available else event.notional)}"
            )
            if remaining.available:
                lines.append(f"📉 <b>Remaining:</b> {fmt_usd(remaining.value)}{marker(remaining)}")
        else:
            lines.append(f"💰 <b>Order value:</b> {fmt_usd_full(event.notional)}")
            size = event.point("size")
            if size.available:
                lines.append(f"📦 <b>Quantity:</b> {fmt_size(size.value)} {coin}")

        filled = event.point("filled_notional")
        if filled.available and float(filled.value) > 0 and not cancelled:
            lines.append(f"✅ <b>Filled:</b> {fmt_usd(filled.value)}{marker(filled)}")
            remaining = event.point("remaining_notional")
            if remaining.available:
                lines.append(f"📉 <b>Remaining:</b> {fmt_usd(remaining.value)}{marker(remaining)}")

        current = event.point("current_px")
        if current.available:
            lines.append(f"📊 <b>Current:</b> {fmt_price(current.value)}")
        distance = event.point("distance_pct")
        if distance.available:
            lines.append(f"📐 <b>Distance:</b> {_distance_text(float(distance.value))}")

        lines.append(f"📌 <b>Status:</b> {escape_html(self._status_label(event))}")

        status_note = event.point("status_note")
        if not status_note.available and status_note.note:
            # We could not resolve cancel-vs-fill; say so rather than guess.
            lines.append(f"⚠️ {escape_html(_short_reason(status_note.note))}")

        resting = event.point("resting_for")
        if resting.available:
            lines.append(f"⏳ <b>Resting:</b> {fmt_duration(float(resting.value))}")

        if event.value("reduce_only"):
            lines.append("↩️ <b>Reduce only:</b> yes")
        if event.has("trigger_px"):
            trigger = event.point("trigger_px")
            lines.append(f"🎚 <b>Trigger:</b> {fmt_price(trigger.value)}{marker(trigger)}")

        lines.append(DIVIDER)
        lines.extend(self._trader_lines(event))
        lines.append(self._time_line(event))
        lines.append(self._verification_line(event))
        lines.append(self._route_line(event))
        lines.append(DIVIDER)
        lines.append(FOOTER)
        return "\n".join(lines)

    # -- aggregate book levels --
    def _render_book(self, event: WhaleEvent) -> str:
        coin = escape_html(event.coin)
        price = event.point("price")
        current = event.point("current_px")
        distance = event.point("distance_pct")
        lines = [
            self._header(event),
            DIVIDER,
            f"🪙 <b>{coin}</b>",
            SIDE_BADGES.get(event.side or "", escape_html(event.side or "N/A")),
            f"💵 <b>Price:</b> {fmt_price(price.value)}" if price.available else "💵 <b>Price:</b> N/A",
            f"💰 <b>Resting:</b> {fmt_usd_full(event.notional)}",
        ]
        orders_at_level = event.point("orders_at_level")
        if orders_at_level.available:
            lines.append(f"🧾 <b>Orders at level:</b> {int(orders_at_level.value)}")
        if current.available:
            lines.append(f"📊 <b>Current:</b> {fmt_price(current.value)}")
        if distance.available:
            lines.append(f"📐 <b>Distance:</b> {_distance_text(float(distance.value))}")
        lines.append(DIVIDER)
        lines.extend(self._trader_lines(event))
        lines.append(self._time_line(event))
        lines.append(self._verification_line(event))
        lines.append(self._route_line(event))
        lines.append(DIVIDER)
        lines.append(FOOTER)
        return "\n".join(lines)

    # -- line builders --
    def _closed_pnl_line(self, event: WhaleEvent) -> str:
        """PnL for a position that no longer exists.

        Three cases, never merged: Hyperliquid's own realised figure
        (``closedPnl`` from the fills feed) is reported as realised; the last
        unrealised PnL we saw before the position went to zero is reported as an
        estimate, because the close may have happened at a different price and
        fees are not included; and if we have neither we say ``N/A``.
        """
        realized = event.point("realized_pnl")
        if realized.available:
            emoji = "🟢" if float(realized.value) >= 0 else "🔴"
            return f"{emoji} <b>Realized PnL:</b> {fmt_usd(realized.value)}{marker(realized)}"
        estimate = event.point("final_unrealized_pnl")
        if estimate.available:
            emoji = "🟢" if float(estimate.value) >= 0 else "🔴"
            return f"{emoji} <b>Final PnL (est.):</b> {fmt_usd(estimate.value)}"
        return "⚪ <b>Final PnL:</b> N/A"

    def _side_line(self, event: WhaleEvent) -> str:
        side = (event.side or "").upper()
        badge = SIDE_BADGES.get(side)
        if badge:
            return badge
        return f"↔️ {escape_html(side)}" if side else "↔️ direction N/A"

    def _liquidated_side_line(self, event: WhaleEvent) -> str:
        """The side of the position that was force-closed, or nothing claimed.

        Only a verified source may fill this in (see
        :meth:`app.whale.detector.WhaleDetector._liquidated_side`). When none was
        available the line says the side is unknown rather than deriving it from
        the fill's BUY/SELL direction, which would be the order/position mistake.
        """
        point = event.point("liquidated_side")
        side = str(point.value).upper() if point.available else ""
        badge = SIDE_BADGES.get(side)
        if badge:
            return f"{badge} liquidated"
        reason = _short_reason(point.note)
        return (
            f"↔️ <b>Liquidated side:</b> N/A — {escape_html(reason)}"
            if reason
            else "↔️ <b>Liquidated side:</b> N/A"
        )

    def _order_side_line(self, event: WhaleEvent) -> str:
        """``🟢 BUY LIMIT`` / ``🔴 SELL LIMIT`` — never LONG or SHORT.

        An order's side is a book side. A resting SELL can belong to a wallet
        with no position, a long that is taking profit, or a short that is
        adding, so translating it into a position direction would be a guess.
        Anything that is not literally BUY or SELL is refused rather than
        rendered, which is what stops a position word from ever leaking here.
        """
        side = (event.side or "").upper()
        if side not in ORDER_SIDES:
            side = ""
        emoji = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "↔️"
        order_type = event.value("order_type")
        kind = str(order_type).upper() if order_type else ("TRIGGER" if event.has("trigger_px") else "LIMIT")
        label = f"{side} {kind}".strip() or "ORDER"
        return f"{emoji} {escape_html(label)}"

    def _value_line(self, event: WhaleEvent) -> str | None:
        """The metric the whale threshold was actually applied to.

        Order notional, position notional, position delta and executed trade
        value are different things, so each is labelled for what it is.
        """
        kind = event.value_kind
        if kind is ValueKind.TRADE_VALUE:
            return f"💰 <b>Executed:</b> {fmt_usd_full(event.notional)}"
        if kind is ValueKind.POSITION_DELTA:
            delta = event.point("delta_value")
            suffix = marker(delta) if delta.available else ""
            if event.event_type is EventType.POSITION_INCREASED:
                return f"➕ <b>Added:</b> {fmt_usd_full(event.notional)}{suffix}"
            if event.event_type is EventType.POSITION_DECREASED:
                return f"➖ <b>Reduced:</b> {fmt_usd_full(event.notional)}{suffix}"
            if event.event_type is EventType.POSITION_FLIPPED:
                return f"🔄 <b>Flipped:</b> {fmt_usd_full(event.notional)}{suffix}"
            return f"💵 <b>Change:</b> {fmt_usd_full(event.notional)}{suffix}"
        if event.event_type is EventType.POSITION_CLOSED:
            return f"💰 <b>Closed:</b> {fmt_usd_full(event.notional)}"
        return None

    def _tpsl_line(self, event: WhaleEvent, key: str, icon: str, label: str) -> str:
        """TP/SL only ever comes from a real resting trigger order."""
        point = event.point(key)
        if point.available:
            return f"{icon} <b>{label}:</b> {fmt_price(point.value)}{marker(point)}"
        note = (point.note or "").lower()
        if "not fetched" in note:
            return f"{icon} <b>{label}:</b> N/A (not checked)"
        return f"{icon} <b>{label}:</b> N/A"

    def _header(self, event: WhaleEvent) -> str:
        """The first line, which has to be true on its own.

        It is what a reader sees in a notification preview, so only something
        that actually executed may be headed ``WHALE TRADE``. Position openings
        and increases lead with the direction only for the position events, whose
        side is read off the last verified ``clearinghouseState`` snapshot that
        still held a position (:meth:`WhaleDetector.from_position_change`) and is
        one of :data:`app.whale.lifecycle.POSITION_SIDES` — never the BUY/SELL of
        whatever execution moved it, because a SELL that trims a long is still a
        long.
        """
        side = (event.side or "").upper()
        directional = DIRECTIONAL_HEADERS.get((event.event_type, side))
        if directional:
            return directional
        return HEADERS.get(event.event_type, "🐋 WHALE ALERT")

    def _trader_lines(self, event: WhaleEvent) -> list[str]:
        """The wallet, in full, on a line of its own.

        Two lines rather than one because a 42-character address and a label do
        not fit together on a phone screen, and a mid-address line break is what
        makes a reader mistake a complete address for a truncated one. The value
        is never shortened anywhere — not here, not in the database, not in a
        list view (spec §21). ``short_wallet`` survives for inline-button labels
        only, where Telegram's 64-byte payload limit is a hard constraint.

        With no wallet the line says so and names the reason: an aggregate book
        level is genuinely anonymous, and inventing an address would be the worst
        available fix (spec §28).
        """
        if not event.wallet:
            reason = _short_reason(event.point("wallet_attribution").note)
            if not reason and event.event_type is EventType.BOOK_LEVEL:
                reason = "aggregated order book"
            return [
                f"👤 <b>Trader:</b> N/A — {escape_html(reason)}"
                if reason
                else "👤 <b>Trader:</b> N/A"
            ]
        # Address only: no real-world identity is claimed for a wallet (spec §20).
        return ["👤 <b>Trader:</b>", f"<code>{escape_html(event.wallet.lower())}</code>"]

    def _verification_line(self, event: WhaleEvent) -> str:
        """What evidence this alert rests on, said in one unambiguous line.

        An execution is labelled ``VERIFIED EXECUTION``; an intention is labelled
        as *not* an execution in those words, so a cancelled $6.83M order can
        never be read as a $6.83M trade (spec §18).
        """
        label = VERIFICATION_LABELS.get(event.event_type)
        if label is None:
            label = "VERIFIED EXECUTION" if event.is_execution else "NOT AN EXECUTION"
        return f"🔎 <b>{label}</b>"

    def _route_line(self, event: WhaleEvent) -> str:
        """Which detection route produced this, and which figure it measured."""
        return f"🧾 <b>Route:</b> {escape_html(self._detection_label(event))}"

    def _time_line(self, event: WhaleEvent) -> str:
        return f"🕐 {fmt_time(event.event_time)}"

    def _status_label(self, event: WhaleEvent) -> str:
        raw = event.value("order_status") or event.status or "unknown"
        return str(raw).upper()

    def _detection_label(self, event: WhaleEvent) -> str:
        detection = event.detection or event.event_type.value.replace("_", " ").title()
        return f"{detection} ({event.value_kind_label.lower()})"

    # ── delivery ──────────────────────────────────────────────
    async def _sender_loop(self) -> None:
        while self._running:
            job = await self._queue.get()
            try:
                await self._dispatch(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad alert must not stop the broadcaster
                log.exception("Alert dispatch failed", extra={"event": job.event_type})
            finally:
                self._queue.task_done()

    async def _dispatch(self, job: _AlertJob) -> None:
        if self.bot is None:
            log.warning("Alert dropped: Telegram bot not attached yet")
            self.dropped += 1
            return

        recipients = await self._resolve_recipients()
        if not recipients:
            # Every admin has either never opened a chat with the bot or has sent
            # /stop, and public mode is off or has no subscribers. That is a
            # legitimate state, but it is also exactly what "411 whale events,
            # 0 alerts delivered" looks like, so it is counted and said out loud
            # (at most once a minute, so a busy feed cannot flood the log).
            self.no_recipients += 1
            self.dropped += 1
            now = utc_now()
            if (
                self._no_recipient_warned_at is None
                or (now - self._no_recipient_warned_at).total_seconds() >= 60
            ):
                self._no_recipient_warned_at = now
                log.warning(
                    "Alert dropped: no recipient resolved. Every admin is either "
                    "unsubscribed (/stop) or has never opened a chat with the bot, "
                    "and public mode adds no subscribers.",
                    extra={
                        "event": job.event_type,
                        "no_recipients_total": self.no_recipients,
                        "admins_known": len(self.admins.admin_ids),
                        "public_mode": self.settings.config.public_mode,
                    },
                )
            return

        results: list[tuple[_Recipient, int | None, str | None]] = []
        for recipient in recipients:
            if not self._buckets.consume(recipient.chat_id):
                self.throttled += 1
                results.append((recipient, None, "throttled: per-chat rate limit"))
                continue
            reply_to = self._thread_root(recipient.chat_id, job.thread_key)
            message_id, error = await self._send(recipient, job.text, reply_to=reply_to)
            if message_id is not None:
                # The root of a thread is its first message: only remember this
                # one when there is nothing to reply to yet.
                self._remember_thread(recipient.chat_id, job.thread_key, message_id, reply_to)
            results.append((recipient, message_id, error))
            await asyncio.sleep(INTER_CHAT_DELAY)

        delivered = [r for r, mid, err in results if err is None]
        if delivered:
            self.sent += len(delivered)
            self.last_sent_at = utc_now()

        await self._record(job, results)

        if self.throttled and self.throttled % 25 == 1:
            log.warning(
                "Alerts are being throttled per chat",
                extra={"throttled_total": self.throttled},
            )

    async def _send(
        self, recipient: _Recipient, text: str, *, reply_to: int | None = None
    ) -> tuple[int | None, str | None]:
        assert self.bot is not None
        for attempt in (1, 2):
            try:
                message = await self.bot.send_message(
                    chat_id=recipient.chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_to_message_id=reply_to,
                )
                return message.message_id, None
            except RetryAfter as exc:
                delay = min(float(getattr(exc, "retry_after", 1.0)) + 0.5, 30.0)
                if attempt == 1:
                    await asyncio.sleep(delay)
                    continue
                self.failed += 1
                return None, f"RetryAfter: {delay:.0f}s"
            except Forbidden:
                # The chat blocked the bot (or was deleted). Stop trying.
                self.blocked += 1
                if not recipient.is_admin:
                    await self._mark_blocked(recipient.telegram_id)
                log.info("Recipient blocked the bot", extra={"chat_id": recipient.chat_id})
                return None, "Forbidden: bot blocked by recipient"
            except BadRequest as exc:
                # The root message may have been deleted by the reader. That must
                # cost the alert itself, not just its thread, so retry unthreaded.
                if reply_to is not None and _is_missing_reply(exc):
                    self._forget_thread(recipient.chat_id, reply_to)
                    log.info(
                        "Reply target is gone; sending the alert unthreaded",
                        extra={"chat_id": recipient.chat_id},
                    )
                    reply_to = None
                    continue
                self.failed += 1
                log.error(
                    "Telegram rejected an alert",
                    extra={"chat_id": recipient.chat_id, "error": str(exc)},
                )
                return None, f"BadRequest: {exc}"
            except TelegramError as exc:
                if attempt == 1:
                    await asyncio.sleep(1.0)
                    continue
                self.failed += 1
                log.warning(
                    "Alert delivery failed",
                    extra={"chat_id": recipient.chat_id, "error": str(exc)},
                )
                return None, f"{type(exc).__name__}: {exc}"
        return None, "unknown delivery failure"

    async def _record(
        self,
        job: _AlertJob,
        results: list[tuple[_Recipient, int | None, str | None]],
    ) -> None:
        """Persist delivery outcomes; also what warms the dedup cache on restart."""
        try:
            async with self.db.session() as session:
                for recipient, message_id, error in results:
                    await AlertRepository.record(
                        session,
                        job.dedup_key or f"{job.event_type}:{job.coin}",
                        event_id=job.event_id,
                        chat_id=recipient.chat_id,
                        message_id=message_id,
                        thread_key=job.thread_key,
                        ok=error is None,
                        error=error,
                    )
                delivered = [r.telegram_id for r, _mid, err in results if err is None]
                if delivered:
                    await UserRepository.bump_alert_counts(session, delivered)
                    if job.event_id is not None:
                        await EventRepository.mark_alerted(session, job.event_id)
        except Exception:
            log.exception("Could not record alert history", extra={"event": job.event_type})

    # ── reply threading ───────────────────────────────────────
    def _thread_root(self, chat_id: int, thread_key: str | None) -> int | None:
        """The message this alert should reply to, if the thread is still fresh."""
        if not thread_key:
            return None
        entry = self._threads.get((chat_id, thread_key))
        if entry is None:
            return None
        message_id, last_used = entry
        if utc_now() - last_used > THREAD_TTL:
            self._threads.pop((chat_id, thread_key), None)
            return None
        return message_id

    def _remember_thread(
        self, chat_id: int, thread_key: str | None, message_id: int, replied_to: int | None
    ) -> None:
        if not thread_key:
            return
        key = (chat_id, thread_key)
        now = utc_now()
        if replied_to is not None:
            # Keep the root and only refresh its recency, so a long-running
            # position stays one flat thread instead of a chain of replies.
            self._threads[key] = (replied_to, now)
            return
        self._threads[key] = (message_id, now)
        while len(self._threads) > THREAD_MAX:
            self._threads.pop(next(iter(self._threads)))

    def _forget_thread(self, chat_id: int, message_id: int) -> None:
        for key, (root, _at) in list(self._threads.items()):
            if key[0] == chat_id and root == message_id:
                self._threads.pop(key, None)

    async def _warm_threads(self) -> None:
        """Restore threading after a restart so a redeploy does not orphan it."""
        try:
            async with self.db.session() as session:
                rows = await AlertRepository.thread_roots(session, utc_now() - THREAD_TTL)
        except Exception:
            log.exception("Could not warm alert reply threads")
            return
        now = utc_now()
        for chat_id, thread_key, message_id in rows:
            # Rows arrive oldest first and ``setdefault`` keeps the first one, so
            # the thread is anchored to its earliest still-recent message rather
            # than chaining replies onto replies.
            self._threads.setdefault((chat_id, thread_key), (message_id, now))
        while len(self._threads) > THREAD_MAX:
            self._threads.pop(next(iter(self._threads)))
        if self._threads:
            log.info("Restored alert reply threads", extra={"threads": len(self._threads)})

    async def _mark_blocked(self, telegram_id: int) -> None:
        try:
            async with self.db.session() as session:
                await UserRepository.mark_blocked(session, telegram_id)
        except Exception:
            log.exception("Could not mark user as blocked", extra={"telegram_id": telegram_id})
        self._recipients_at = None

    # ── recipients ────────────────────────────────────────────
    def invalidate_recipients(self) -> None:
        """Call after a subscription or admin change."""
        self._recipients_at = None

    async def _resolve_recipients(self) -> list[_Recipient]:
        now = utc_now()
        if (
            self._recipients_at is not None
            and (now - self._recipients_at).total_seconds() < RECIPIENT_CACHE_TTL
        ):
            return self._recipients

        recipients: list[_Recipient] = []
        seen: set[int] = set()

        # Explicit opt-outs (/stop, or the bot blocked) are honoured for everyone,
        # administrators included: "send me nothing" is a personal preference, not
        # a permission level. A user with no row has never opted out.
        opted_out: set[int] = set()
        try:
            async with self.db.session() as session:
                opted_out = await UserRepository.unsubscribed_ids(session)
        except Exception:
            log.exception("Could not load alert opt-outs; alerting everyone")

        # Administrators receive alerts unless they opted out. A Telegram user's
        # private chat id equals their user id, so no lookup is needed.
        for admin_id in self.admins.admin_ids:
            if admin_id and admin_id not in seen and admin_id not in opted_out:
                seen.add(admin_id)
                recipients.append(_Recipient(admin_id, admin_id, True))

        if self.settings.config.public_mode:
            try:
                async with self.db.session() as session:
                    for user in await UserRepository.subscribers(session):
                        if user.telegram_id in seen or user.telegram_id in opted_out:
                            continue
                        seen.add(user.telegram_id)
                        recipients.append(
                            _Recipient(user.chat_id or user.telegram_id, user.telegram_id, False)
                        )
            except Exception:
                log.exception("Could not load alert subscribers")

        self._recipients = recipients
        self._recipients_at = now
        return recipients

    # ── observability ─────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        return {
            "queued": self.queued,
            "queue_depth": self._queue.qsize(),
            "dropped": self.dropped,
            "sent": self.sent,
            "failed": self.failed,
            "throttled": self.throttled,
            "blocked": self.blocked,
            "no_recipients": self.no_recipients,
            "paused_drops": self.paused_drops,
            "recipients": len(self._recipients),
            "threads": len(self._threads),
            "bot_attached": self.bot is not None,
            "last_sent_at": self.last_sent_at.isoformat() if self.last_sent_at else None,
        }
