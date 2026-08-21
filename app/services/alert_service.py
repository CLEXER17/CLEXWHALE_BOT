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
always receive alerts; ordinary subscribers receive them only while public mode
is on. Each chat has its own token bucket, every send is written to
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
    short_wallet,
    utc_now,
)
from app.utils.logging import get_logger
from app.utils.ratelimit import TokenBucket
from app.whale.events import EventType, ValueKind, WhaleEvent

log = get_logger(__name__)

QUEUE_MAX = 500
#: Pause between consecutive chats so a broadcast does not burst Telegram.
INTER_CHAT_DELAY = 0.06
RECIPIENT_CACHE_TTL = 30.0
FOOTER = "🐋 Whale Monitor"

#: Header per event type. Position openings and large trades use the canonical
#: signal format; lifecycle changes get their own headline.
HEADERS: dict[EventType, str] = {
    EventType.WHALE_TRADE: "🐋 HYPERLIQUID WHALE ALERT",
    EventType.POSITION_OPENED: "🐋 HYPERLIQUID WHALE ALERT",
    EventType.POSITION_INCREASED: "🐋 WHALE POSITION INCREASED",
    EventType.POSITION_DECREASED: "🐋 WHALE POSITION REDUCED",
    EventType.POSITION_CLOSED: "🐋 WHALE POSITION CLOSED",
    EventType.POSITION_FLIPPED: "🔄 WHALE POSITION FLIPPED",
    EventType.ORDER_PLACED: "🐋 LARGE LIMIT ORDER",
    EventType.ORDER_MODIFIED: "✏️ WHALE ORDER MODIFIED",
    EventType.ORDER_PARTIALLY_FILLED: "⏳ WHALE ORDER PARTIALLY FILLED",
    EventType.ORDER_FILLED: "✅ WHALE ORDER FILLED",
    EventType.ORDER_CANCELLED: "🚨 WHALE ORDER CANCELLED",
    EventType.ORDER_REJECTED: "⛔ WHALE ORDER REJECTED",
    EventType.BOOK_LEVEL: "🐋 LARGE BOOK LEVEL",
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


@dataclass(slots=True)
class _AlertJob:
    event_type: str
    coin: str
    dedup_key: str
    text: str
    event_id: int | None = None
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

        # counters surfaced through /status and /health
        self.queued = 0
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self.throttled = 0
        self.blocked = 0
        self.last_sent_at: datetime | None = None

    # ── lifecycle ─────────────────────────────────────────────
    def attach_bot(self, bot: Bot) -> None:
        """Wire in the Telegram bot once the application has been built."""
        self.bot = bot

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
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
        if event.is_order_event:
            return self._render_order(event)
        return self._render_position(event)

    # -- position / trade signals (spec §16) --
    def _render_position(self, event: WhaleEvent) -> str:
        coin = escape_html(event.coin)
        lines = [
            HEADERS.get(event.event_type, "🐋 HYPERLIQUID WHALE ALERT"),
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
                f"💰 <b>Position:</b> {fmt_usd_full(position_value.value)}{marker(position_value)}"
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
        distance = event.point("distance_pct")
        if distance.available:
            lines.append(f"📐 <b>Distance:</b> {fmt_pct(distance.value)}")

        if live_position or event.has("take_profit_px") or event.has("stop_loss_px"):
            lines.append(self._tpsl_line(event, "take_profit_px", "🎯", "TP"))
            lines.append(self._tpsl_line(event, "stop_loss_px", "🛑", "SL"))

        size = event.point("position_size")
        if not size.available:
            size = event.point("size")
        if size.available:
            lines.append(f"📦 <b>Size:</b> {fmt_size(size.value)} {coin}")

        pnl = event.point("unrealized_pnl")
        if event.event_type is EventType.POSITION_CLOSED:
            pnl = event.point("final_unrealized_pnl")
        if pnl.available:
            label = "Final PnL" if event.event_type is EventType.POSITION_CLOSED else "Unrealised"
            emoji = "🟢" if float(pnl.value) >= 0 else "🔴"
            lines.append(f"{emoji} <b>{label}:</b> {fmt_usd(pnl.value)}{marker(pnl)}")

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
            reason = _short_reason(position_value.note) or "wallet not enriched yet"
            lines.append(f"ℹ️ <b>Position data:</b> unavailable — {escape_html(reason)}")

        lines.append(DIVIDER)
        lines.append(self._trader_line(event))
        lines.append(self._time_line(event))
        lines.append(f"🔎 <b>Detection:</b> {escape_html(self._detection_label(event))}")
        lines.append(DIVIDER)
        lines.append(FOOTER)
        return "\n".join(lines)

    # -- resting order alerts (spec §17 / §18) --
    def _render_order(self, event: WhaleEvent) -> str:
        coin = escape_html(event.coin)
        cancelled = event.event_type is EventType.ORDER_CANCELLED
        lines = [
            HEADERS.get(event.event_type, "🐋 LARGE LIMIT ORDER"),
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
            lines.append(f"💰 <b>Size:</b> {fmt_usd_full(event.notional)}")
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
            lines.append(f"📐 <b>Distance:</b> {fmt_pct(distance.value)}")

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

        lines.append(self._trader_line(event))
        lines.append(f"🕐 {fmt_time(event.event_time)}")
        lines.append(f"🔎 <b>Detection:</b> {escape_html(self._detection_label(event))}")
        lines.append(DIVIDER)
        return "\n".join(lines)

    # -- aggregate book levels --
    def _render_book(self, event: WhaleEvent) -> str:
        coin = escape_html(event.coin)
        price = event.point("price")
        current = event.point("current_px")
        distance = event.point("distance_pct")
        attribution = event.point("wallet_attribution")
        lines = [
            HEADERS[EventType.BOOK_LEVEL],
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
            lines.append(f"📐 <b>Distance:</b> {fmt_pct(distance.value)}")
        lines.append(f"👤 <b>Trader:</b> N/A — {escape_html(_short_reason(attribution.note))}")
        lines.append(f"🕐 {fmt_time(event.event_time)}")
        lines.append(DIVIDER)
        return "\n".join(lines)

    # -- line builders --
    def _side_line(self, event: WhaleEvent) -> str:
        side = (event.side or "").upper()
        badge = SIDE_BADGES.get(side)
        if badge:
            return badge
        return f"↔️ {escape_html(side)}" if side else "↔️ direction N/A"

    def _order_side_line(self, event: WhaleEvent) -> str:
        side = (event.side or "").upper()
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
            return f"💱 <b>Trade:</b> {fmt_usd_full(event.notional)}"
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

    def _trader_line(self, event: WhaleEvent) -> str:
        if not event.wallet:
            return "👤 <b>Trader:</b> N/A"
        # Address only: no identity is claimed for any wallet (spec §20).
        return f"👤 <b>Trader:</b> <code>{escape_html(short_wallet(event.wallet))}</code>"

    def _time_line(self, event: WhaleEvent) -> str:
        label = "Opened" if event.event_type is EventType.POSITION_OPENED else "Detected"
        return f"🕐 <b>{label}:</b> {fmt_time(event.event_time)}"

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
            log.debug("Alert has no recipients", extra={"event": job.event_type})
            return

        results: list[tuple[_Recipient, int | None, str | None]] = []
        for recipient in recipients:
            if not self._buckets.consume(recipient.chat_id):
                self.throttled += 1
                results.append((recipient, None, "throttled: per-chat rate limit"))
                continue
            message_id, error = await self._send(recipient, job.text)
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

    async def _send(self, recipient: _Recipient, text: str) -> tuple[int | None, str | None]:
        assert self.bot is not None
        for attempt in (1, 2):
            try:
                message = await self.bot.send_message(
                    chat_id=recipient.chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
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

        # Administrators always receive alerts. A Telegram user's private chat id
        # equals their user id, so no lookup is needed.
        for admin_id in self.admins.admin_ids:
            if admin_id and admin_id not in seen:
                seen.add(admin_id)
                recipients.append(_Recipient(admin_id, admin_id, True))

        if self.settings.config.public_mode:
            try:
                async with self.db.session() as session:
                    for user in await UserRepository.subscribers(session):
                        if user.telegram_id in seen:
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
            "recipients": len(self._recipients),
            "bot_attached": self.bot is not None,
            "last_sent_at": self.last_sent_at.isoformat() if self.last_sent_at else None,
        }
