"""Ingestion and detection orchestration.

Three tiers, chosen to fit Hyperliquid's documented limits:

1. **Discovery — global websocket.** One connection subscribes to ``trades``
   for every monitored coin plus ``allMids`` for prices. ``trades`` is the only
   global feed that carries wallet addresses (``users: [buyer, seller]``), so it
   is what finds whales in the first place.
2. **Enrichment — weight-budgeted REST.** ``clearinghouseState`` (weight 2) gives
   real position size, entry price, liquidation price, leverage and margin;
   ``frontendOpenOrders`` (weight 20) gives resting orders and the trader's own
   TP/SL trigger levels; ``orderStatus`` (weight 2) resolves whether a vanished
   order was cancelled or filled. All of it runs inside a 1200 weight/minute
   budget with a configurable safety factor, so we degrade by fetching *less*
   rather than by getting rate-limited.
3. **Focus slate — per-wallet websockets.** The highest-scoring wallets get a
   live ``orderUpdates`` stream. Hyperliquid allows 10 unique addresses across
   all user subscriptions per IP and 10 connections, and an ``orderUpdates``
   frame does not name its user, so each focus wallet gets its own connection
   and the slate is capped accordingly.

The websocket handler never blocks: it prefilters and hands work to a queue that
worker tasks drain, so Hyperliquid ingestion cannot stall Telegram or the HTTP
health server. Every event then goes through the same pipeline — filter →
deduplicate → persist → alert — and a failure at any stage is logged and
contained.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from app.config import WS_UNIQUE_USER_HARD_CAP, Settings
from app.database.base import Database
from app.database.repository import (
    AlertRepository,
    EventRepository,
    OrderRepository,
    PositionRepository,
    WalletRepository,
)
from app.hyperliquid import parser
from app.hyperliquid.constants import MAX_WS_CONNECTIONS
from app.hyperliquid.models import AccountState, L2Book, OpenOrder, OrderUpdate, Trade
from app.hyperliquid.rest import HyperliquidREST
from app.hyperliquid.websocket import HyperliquidWebSocket
from app.services.settings_service import RuntimeConfig, SettingsService
from app.utils.formatting import DataPoint, utc_now
from app.utils.logging import get_logger
from app.whale.dedup import IDENTITY_TTL, Deduplicator
from app.whale.detector import OrderState, WhaleDetector
from app.whale.events import EventType, WhaleEvent
from app.whale.filters import WhaleFilter
from app.whale.lifecycle import POSITION_SIDES, may_modify_position
from app.whale.tracker import WalletTracker, order_state_from_open_order

log = get_logger(__name__)

AlertCallback = Callable[[WhaleEvent, int | None], Awaitable[None]]

#: Wallets are tracked (for scoring / focus election) at a fraction of the
#: alert threshold, so the slate is chosen from a wider pool than we alert on.
DISCOVERY_FACTOR = 0.5

QUEUE_MAX = 2000
WORKERS = 3
UNIVERSE_REFRESH = 300.0
FOCUS_REFRESH = 60.0
MAINTENANCE_INTERVAL = 300.0
POSITION_TICK = 2.0
ORDER_TICK = 5.0
BOOK_TICK = 30.0

#: One connection is reserved for the market feed; keep one spare.
MAX_FOCUS_WALLETS = min(WS_UNIQUE_USER_HARD_CAP, MAX_WS_CONNECTIONS - 2)

#: Cap on the pending realised-PnL map (wallet+coin → summed ``closedPnl``).
REALIZED_PNL_MAX = 500

#: Weight that must be left in the sliding window before an *opportunistic*
#: ``frontendOpenOrders`` (weight 20) is worth spending. Routine polling keeps a
#: wide reserve so a burst of weight-2 position polls is never starved by it.
ORDER_POLL_RESERVE = 200.0

#: The reserve when that same call backs an alert being rendered right now. It is
#: the only public source of the trader's own TP/SL levels, so the bar here is
#: "the call fits, with room for the weight-2 follow-ups in the same cycle"
#: rather than "there is plenty of slack". Under the wide reserve, a whale alert
#: that happened to arrive during a busy minute printed `N/A (not checked)` for
#: TP and SL — data Hyperliquid would have given us for 20 of a 1200 budget.
ALERT_ORDER_POLL_RESERVE = 60.0


@dataclass(slots=True)
class _Job:
    kind: str
    payload: Any
    wallet: str | None = None


class WhaleEngine:
    def __init__(
        self,
        env: Settings,
        database: Database,
        settings: SettingsService,
        rest: HyperliquidREST,
        alert_callback: AlertCallback | None = None,
    ) -> None:
        self.env = env
        self.db = database
        self.settings = settings
        self.rest = rest
        self.alert_callback = alert_callback

        self.tracker = WalletTracker(
            max_wallets=env.wallet_cache_size,
            idle_ttl=env.wallet_idle_ttl,
            position_interval=env.position_poll_interval,
            order_interval=env.order_poll_interval,
        )
        self.detector = WhaleDetector(price_provider=self.price_of)
        self.filter = WhaleFilter(settings)
        self.dedup = Deduplicator()

        self.market_ws = HyperliquidWebSocket(
            env.hyperliquid_ws_url, self._on_market_message, name="market"
        )
        self._user_ws: dict[str, HyperliquidWebSocket] = {}

        self._prices: dict[str, float] = {}
        #: (wallet, coin) → summed ``closedPnl`` awaiting the next close. Only
        #: the per-user fills feed populates this, so it covers the focus slate.
        self._realized_pnl: dict[tuple[str, str], float] = {}
        self._universe: tuple[str, ...] = ()
        self._known_coins: set[str] = set()
        self._volumes: dict[str, float] = {}
        self._dropped_coins: tuple[str, ...] = ()

        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._tasks: list[asyncio.Task[None]] = []
        # Several workers run concurrently and two events from the same wallet can
        # be in flight at once. ``wallets`` and ``positions`` are read-then-write,
        # so unsynchronised sessions race: one transaction loses on the primary
        # key and its event — and therefore its alert — is dropped. The write is
        # short and off the enrichment path, so serialising it costs nothing.
        self._write_lock = asyncio.Lock()
        self._config_changed = asyncio.Event()
        self._running = False
        #: Last pause state acted on, so a settings change that did not touch the
        #: pause does not tear the feeds down and rebuild them.
        self._paused_seen = settings.config.paused
        self.started_at: datetime | None = None

        # counters
        self.trades_seen = 0
        self.candidates = 0
        self.events_detected = 0
        self.events_alerted = 0
        self.queue_dropped = 0
        self.persist_errors = 0
        #: Events the durable gate caught that the in-memory cache had forgotten.
        self.duplicate_persisted = 0
        #: Position writes refused because no verified LONG/SHORT side was known.
        self.position_writes_skipped = 0
        self.last_event_at: datetime | None = None
        self.last_alert_at: datetime | None = None

        settings.on_change(self._on_config_change)

    # ── configuration ─────────────────────────────────────────
    @property
    def config(self) -> RuntimeConfig:
        return self.settings.config

    async def _on_config_change(self, config: RuntimeConfig) -> None:
        self.tracker.pin(list(config.tracked_wallets))
        self._config_changed.set()
        if config.paused == self._paused_seen:
            return
        # A global pause must take effect now, not at the next loop tick: drop
        # every market subscription and release every focus socket immediately,
        # and re-establish them on resume.
        self._paused_seen = config.paused
        if not self._running:
            return
        try:
            await self._apply_market_subscriptions()
            await self._apply_focus_slate()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Could not apply the pause state to the feeds",
                extra={"paused": config.paused},
            )
        log.info("Global pause applied" if config.paused else "Global pause lifted")

    def price_of(self, coin: str) -> float | None:
        return self._prices.get(coin.upper())

    # ── lifecycle ─────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.started_at = utc_now()

        await self._warm_dedup()
        self.tracker.pin(list(self.config.tracked_wallets))
        await self._refresh_universe()
        await self._apply_market_subscriptions()
        await self.market_ws.start()

        for index in range(WORKERS):
            self._spawn(self._worker(index), f"whale-worker-{index}")
        self._spawn(self._universe_loop(), "whale-universe")
        self._spawn(self._position_loop(), "whale-positions")
        self._spawn(self._order_loop(), "whale-orders")
        self._spawn(self._focus_loop(), "whale-focus")
        self._spawn(self._book_loop(), "whale-book")
        self._spawn(self._maintenance_loop(), "whale-maintenance")
        log.info(
            "Whale engine started",
            extra={
                "coins": len(self._universe),
                "monitoring": self.config.monitoring_enabled,
                "threshold": self.config.min_whale_value,
            },
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        await self.market_ws.stop()
        for socket in list(self._user_ws.values()):
            await socket.stop()
        self._user_ws.clear()
        log.info("Whale engine stopped")

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))  # type: ignore[arg-type]

    async def _warm_dedup(self) -> None:
        """Reload recently alerted identities so a redeploy is not a replay."""
        try:
            async with self.db.session() as session:
                keys = await AlertRepository.recent_keys(session, utc_now() - timedelta(hours=1))
            self.dedup.warm(keys)
        except Exception:
            log.exception("Could not warm the dedup cache from alert history")

    # ── coin universe ─────────────────────────────────────────
    async def _refresh_universe(self) -> None:
        metas, contexts = await self.rest.meta_and_asset_ctxs()
        if metas:
            self._known_coins = {meta.name for meta in metas}
        for coin, ctx in contexts.items():
            price = ctx.reference_price
            if price:
                self._prices[coin] = price
            if ctx.day_ntl_vlm is not None:
                self._volumes[coin] = ctx.day_ntl_vlm

        cfg = self.config
        if cfg.all_coins:
            ranked = sorted(
                self._known_coins or set(self._volumes),
                key=lambda coin: self._volumes.get(coin, 0.0),
                reverse=True,
            )
            cap = max(1, cfg.max_monitored_coins)
            selected = ranked[:cap]
            dropped = ranked[cap:]
            self._dropped_coins = tuple(dropped)
            if dropped:
                # Never silently truncate coverage (spec §55).
                log.info(
                    "Coin universe capped by MAX_MONITORED_COINS",
                    extra={
                        "monitored": len(selected),
                        "cap": cap,
                        "not_monitored": len(dropped),
                        "examples_excluded": dropped[:5],
                    },
                )
        else:
            selected = [coin for coin in cfg.coins if not self._known_coins or coin in self._known_coins]
            unknown = [coin for coin in cfg.coins if self._known_coins and coin not in self._known_coins]
            self._dropped_coins = tuple(unknown)
            if unknown:
                log.warning(
                    "Selected coins are not Hyperliquid perpetuals and cannot be monitored",
                    extra={"unknown": unknown},
                )

        if not selected and not cfg.all_coins:
            log.warning("No monitorable coins selected; no market feeds will be subscribed")
        self._universe = tuple(selected)

    def _desired_market_subscriptions(self) -> list[dict[str, Any]]:
        cfg = self.config
        if not cfg.monitoring_active:
            return []
        subs: list[dict[str, Any]] = [{"type": "allMids"}]
        for coin in self._universe:
            subs.append({"type": "trades", "coin": coin})
        if cfg.enable_book_scanner:
            for coin in self._universe[:20]:
                subs.append({"type": "l2Book", "coin": coin})
        return subs

    async def _apply_market_subscriptions(self) -> None:
        await self.market_ws.replace_subscriptions(self._desired_market_subscriptions())

    # ── websocket handlers (must not block) ───────────────────
    async def _on_market_message(self, channel: str, data: Any) -> None:
        if channel == "allMids":
            mids = parser.parse_all_mids(data)
            if mids:
                self._prices.update(mids)
            return
        if channel == "trades":
            for trade in parser.parse_trades(data):
                self._on_trade(trade)
            return
        if channel == "l2Book":
            book = parser.parse_l2_book(data)
            if book is not None and self.config.enable_book_scanner:
                self._offer(_Job("book", book))
            return

    async def _on_user_message(self, wallet: str, channel: str, data: Any) -> None:
        if channel == "orderUpdates":
            for update in parser.parse_order_updates(data):
                self._offer(_Job("order_update", update, wallet=wallet))
            return
        if channel == "userEvents" and isinstance(data, dict):
            # Liquidation detail is only exposed per user; surface it as a fill.
            fills = parser.parse_fills(data.get("fills"))
            for fill in fills:
                self._record_realized_pnl(wallet, fill)
                if fill.is_liquidation:
                    self._offer(_Job("liquidation", fill, wallet=wallet))
            return

    def _record_realized_pnl(self, wallet: str, fill: Any) -> None:
        """Accumulate Hyperliquid's own ``closedPnl`` for one wallet+coin.

        This is the only *realised* PnL the API offers, and it arrives only on
        the per-user fills feed — so it exists for the focus slate and for
        nothing else. It is consumed by the next close of that wallet+coin and
        then discarded. When it is absent the alert says so; nothing is inferred.
        """
        closed_pnl = getattr(fill, "closed_pnl", None)
        if not wallet or closed_pnl is None:
            return
        key = (wallet.lower(), fill.coin)
        self._realized_pnl[key] = self._realized_pnl.get(key, 0.0) + float(closed_pnl)
        while len(self._realized_pnl) > REALIZED_PNL_MAX:
            self._realized_pnl.pop(next(iter(self._realized_pnl)))

    def _attach_realized_pnl(self, event: WhaleEvent) -> None:
        """Give a close its verified realised PnL, if the fills feed provided one."""
        if not event.wallet:
            return
        value = self._realized_pnl.pop((event.wallet.lower(), event.coin), None)
        if value is None:
            return
        event.set(
            "realized_pnl",
            DataPoint.confirmed(value, "sum of closedPnl reported on this wallet's fills"),
        )

    def _on_trade(self, trade: Trade) -> None:
        self.trades_seen += 1
        cfg = self.config
        if not cfg.monitoring_active or not cfg.enable_trade_detector:
            return
        threshold = cfg.threshold_for("trade")
        notional = trade.notional
        if notional < threshold * DISCOVERY_FACTOR:
            return
        if not cfg.coin_enabled(trade.coin):
            return

        # Both sides of a large trade are worth tracking; only the aggressor is
        # alerted here. The maker's side arrives through the order lifecycle.
        when = trade.time or utc_now()
        if cfg.enable_wallet_tracking:
            for address in trade.participants:
                self.tracker.observe_trade(address, trade.coin, notional, when)

        if notional < threshold:
            return
        self.candidates += 1
        self._offer(_Job("trade", trade))

    def _offer(self, job: _Job) -> None:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.queue_dropped += 1
            if self.queue_dropped % 50 == 1:
                log.warning(
                    "Detection queue full; dropping events",
                    extra={"dropped_total": self.queue_dropped, "kind": job.kind},
                )

    # ── workers ───────────────────────────────────────────────
    async def _worker(self, index: int) -> None:
        while self._running:
            job = await self._queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad event must never kill the pipeline
                log.exception("Job processing failed", extra={"kind": job.kind})
            finally:
                self._queue.task_done()

    async def _process(self, job: _Job) -> None:
        if job.kind == "trade":
            await self._process_trade(job.payload)
        elif job.kind == "order_update":
            await self._process_order_update(job.wallet or "", job.payload)
        elif job.kind == "book":
            await self._process_book(job.payload)
        elif job.kind == "liquidation":
            await self._process_liquidation(job.wallet or "", job.payload)

    async def _process_trade(self, trade: Trade) -> None:
        wallet = trade.taker or trade.buyer or trade.seller
        if wallet:
            # Every trade that reaches here has already cleared the alert
            # threshold in ``_on_trade``, so this enrichment is what the alert
            # will be rendered from — worth the weight-20 trigger-order fetch
            # that populates TP/SL.
            await self._enrich(wallet, trade.coin, for_alert=True)
        context = self.tracker.context_for(wallet, trade.coin) if wallet else None
        event = self.detector.from_trade(trade, wallet=wallet, context=context)
        if event is not None:
            await self._emit(event)

    async def _process_order_update(self, wallet: str, update: OrderUpdate) -> None:
        if not wallet or not self.config.enable_order_detector:
            return
        state = OrderState(
            oid=update.oid,
            coin=update.coin,
            side=update.side,
            limit_px=update.limit_px,
            size=update.sz,
            orig_size=update.orig_sz,
            notional=update.notional,
            status=update.status,
            placed_at=update.timestamp,
        )
        previous = self.tracker.apply_order_state(wallet, state)
        event = self.detector.from_order_update(
            wallet, update, previous, min_notional=self.config.lowest_threshold
        )
        if event is not None:
            await self._emit(event)

    async def _process_book(self, book: L2Book) -> None:
        cfg = self.config
        if not cfg.enable_book_scanner:
            return
        for event in self.detector.from_book(book, min_notional=cfg.threshold_for("order")):
            await self._emit(event)

    async def _process_liquidation(self, wallet: str, fill: Any) -> None:
        """A liquidation fill for a focus wallet. Hyperliquid exposes liquidation
        detail only on per-user feeds, so this covers the focus slate only.

        Order of operations matters. The snapshot is read **before** the forced
        refetch, because the refetch is what replaces the liquidated position with
        a flat one — after it, the only thing that could tell us which side was
        closed is gone. The event is emitted from that pre-liquidation snapshot,
        and then the refetch runs so the normal snapshot diff produces the real
        ``POSITION_CLOSED``. Two alerts, two different facts: the exchange forced
        a close, and the position is now flat.
        """
        context = self.tracker.context_for(wallet, fill.coin) if wallet else None
        event = self.detector.from_liquidation(
            wallet,
            fill,
            context=context,
            min_notional=self.config.threshold_for("position"),
        )
        if event is not None:
            await self._emit(event)
        await self._enrich(wallet, fill.coin, force=True)

    # ── enrichment ────────────────────────────────────────────
    async def _enrich(
        self, wallet: str, coin: str, *, force: bool = False, for_alert: bool = False
    ) -> None:
        """Fetch what we can afford about a wallet, without blocking on budget.

        ``force`` ignores the poll intervals — the caller has evidence the state
        just changed. ``for_alert`` says an alert for this wallet is being built
        right now, which changes only how much weight we are willing to spend:
        the poll intervals still apply, because a cached snapshot seconds old is
        just as good and the alert should not pay for a re-fetch.
        """
        tracked = self.tracker.get(wallet)
        now = utc_now()
        backs_alert = force or for_alert

        position_age = (
            (now - tracked.positions_at).total_seconds()
            if tracked is not None and tracked.positions_at is not None
            else None
        )
        if force or position_age is None or position_age > self.env.position_poll_interval:
            state = await self.rest.clearinghouse_state(wallet, wait_for_budget=False)
            if state is not None:
                await self._ingest_account_state(state)
            else:
                self.tracker.record_failure(wallet, backoff=30.0)

        # ``frontendOpenOrders`` serves two responsibilities and only one of them
        # is order *detection*: it is also the sole source of the TP/SL levels a
        # position alert prints. Conflating the two meant a user who turned the
        # order detector off (they did not want resting-order alerts) also lost
        # TP/SL on every whale alert. So the fetch is still made when it backs an
        # alert; ``_ingest_open_orders`` re-checks the flag before emitting, so no
        # order event appears from a disabled detector.
        if not self.config.enable_order_detector and not backs_alert:
            return
        order_age = (
            (now - tracked.orders_at).total_seconds()
            if tracked is not None and tracked.orders_at is not None
            else None
        )
        reserve = ALERT_ORDER_POLL_RESERVE if backs_alert else ORDER_POLL_RESERVE
        if (force or order_age is None or order_age > self.env.order_poll_interval) and (
            self.rest.limiter.available > reserve
        ):
            orders = await self.rest.frontend_open_orders(wallet, wait_for_budget=False)
            if orders is not None:
                await self._ingest_open_orders(wallet, orders)

    async def _ingest_account_state(self, state: AccountState) -> None:
        """Apply a position snapshot and emit real lifecycle changes.

        The very first snapshot for a wallet only establishes a baseline: a
        position that already existed when we discovered the wallet is not
        announced as "just opened", because we have no evidence of when it was
        opened.
        """
        wallet = state.user
        previous, current = self.tracker.apply_positions(
            wallet, state.positions, account_value=state.account_value
        )
        if previous is None:
            log.debug(
                "Position baseline recorded",
                extra={"wallet": wallet, "positions": len(current)},
            )
            return
        if not self.config.enable_position_detector:
            return

        prefilter = self.config.lowest_threshold
        for coin in sorted(set(previous) | set(current)):
            event = self.detector.from_position_change(
                wallet,
                coin,
                previous.get(coin),
                current.get(coin),
                context=self.tracker.context_for(wallet, coin),
                min_notional=prefilter,
            )
            if event is not None:
                if event.event_type is EventType.POSITION_CLOSED:
                    self._attach_realized_pnl(event)
                await self._emit(event)

    async def _ingest_open_orders(self, wallet: str, orders: list[OpenOrder]) -> None:
        previous, current = self.tracker.apply_orders(wallet, orders)
        if previous is None or not self.config.enable_order_detector:
            return

        prefilter = self.config.lowest_threshold
        for order in orders:
            event = self.detector.from_open_order(
                wallet, order, previous.get(order.oid), min_notional=prefilter
            )
            if event is not None:
                await self._emit(event)

        vanished = [state for oid, state in previous.items() if oid not in current]
        for state in vanished:
            if state.notional < prefilter:
                continue
            resolved = await self._resolve_order_status(wallet, state.oid)
            event = self.detector.from_order_disappearance(
                wallet, state, resolved, min_notional=prefilter
            )
            if event is not None:
                await self._emit(event)

    async def _resolve_order_status(self, wallet: str, oid: int) -> str | None:
        """Ask Hyperliquid what actually happened to an order (weight 2)."""
        raw = await self.rest.order_status(wallet, oid)
        if not isinstance(raw, dict):
            return None
        if raw.get("status") != "order":
            return None
        payload = raw.get("order")
        if isinstance(payload, dict):
            status = payload.get("status")
            if isinstance(status, str):
                return status
        return None

    # ── pipeline ──────────────────────────────────────────────
    async def _emit(self, event: WhaleEvent) -> None:
        self.events_detected += 1
        self.last_event_at = utc_now()

        decision = self.filter.evaluate(event)
        if not decision.accepted:
            return
        if not self.dedup.check(event, self.config.alert_cooldown_seconds):
            return
        if await self._already_recorded(event):
            self.duplicate_persisted += 1
            return

        event_id = await self._persist(event)
        if event_id is None:
            # Persistence failed: forget the identity so a retry can still alert.
            self.dedup.forget(event)
            return

        self.tracker.record_alert(event.wallet)
        self.events_alerted += 1
        self.last_alert_at = utc_now()

        if self.alert_callback is not None:
            try:
                await self.alert_callback(event, event_id)
            except Exception:
                log.exception("Alert callback failed", extra={"event": event.event_type.value})

    async def _already_recorded(self, event: WhaleEvent) -> bool:
        """Durable duplicate gate, behind the in-memory one.

        ``Deduplicator`` is a bounded TTL cache in RAM, so it forgets across a
        restart and evicts under pressure — and a redeploy is exactly when the
        websocket replays its snapshots. ``whale_events.dedup_key`` is the record
        of what has actually been recorded, so it is consulted before writing.

        A database problem here must not silence detection: on error the answer is
        "not seen", and the in-memory gate stands alone.
        """
        if not event.dedup_key:
            return False
        since = utc_now() - timedelta(seconds=IDENTITY_TTL)
        try:
            async with self.db.session() as session:
                return await EventRepository.seen_recently(session, event.dedup_key, since)
        except Exception:
            log.debug(
                "Durable duplicate check unavailable; relying on the in-memory cache",
                extra={"event": event.event_type.value},
            )
            return False

    async def _persist(self, event: WhaleEvent) -> int | None:
        try:
            async with self._write_lock, self.db.session() as session:
                row = await EventRepository.insert(session, **event.db_fields())
                if event.wallet:
                    await WalletRepository.record_activity(
                        session,
                        event.wallet,
                        coin=event.coin,
                        side=event.side,
                        notional=event.notional,
                        position_value=event.numeric("position_value"),
                        order_value=event.numeric("orig_notional") if event.is_order_event else None,
                        account_value=event.numeric("account_value"),
                        seen_at=event.event_time,
                    )
                    if event.is_order_event:
                        await self._persist_order(session, event)
                    elif may_modify_position(event):
                        # Only verified position data writes position state: an
                        # order event never does, and a bare execution with no
                        # snapshot behind it never does either.
                        await self._persist_position(session, event)
                return int(row.id)
        except Exception:
            self.persist_errors += 1
            log.exception("Failed to persist whale event", extra={"event": event.event_type.value})
            return None

    async def _persist_order(self, session: Any, event: WhaleEvent) -> None:
        if event.order_id is None or not event.wallet:
            return
        closed = event.event_type in {
            EventType.ORDER_CANCELLED,
            EventType.ORDER_FILLED,
            EventType.ORDER_REJECTED,
        }
        await OrderRepository.upsert(
            session,
            event.wallet,
            event.order_id,
            coin=event.coin,
            side=event.side,
            limit_px=event.numeric("price"),
            size=event.numeric("size"),
            orig_size=event.numeric("orig_size"),
            notional=abs(event.notional),
            orig_notional=event.numeric("orig_notional"),
            order_type=event.value("order_type"),
            is_trigger=bool(event.value("trigger_px")),
            trigger_px=event.numeric("trigger_px"),
            reduce_only=bool(event.value("reduce_only")),
            status=event.status or "open",
            placed_at=None,
        )
        if closed:
            await OrderRepository.close(
                session, event.wallet, event.order_id, event.status or "closed", event.event_time
            )

    async def _persist_position(self, session: Any, event: WhaleEvent) -> None:
        if not event.wallet:
            return
        size = event.numeric("position_size")
        if size is None and event.is_position_event:
            size = event.numeric("size")
        position_value = event.numeric("position_value")
        closed = event.event_type is EventType.POSITION_CLOSED
        existing = await PositionRepository.get(session, event.wallet, event.coin)

        # ORDER ≠ POSITION, and an execution side is not a position side: a BUY
        # is not evidence of a long. Only ``LONG``/``SHORT`` from a
        # clearinghouseState snapshot may be written here. This is what produced
        # rows reading "BTC BUY — Notional: N/A": an execution side reached the
        # positions table without a snapshot behind it.
        side = event.value("position_side")
        if side not in POSITION_SIDES:
            side = event.side if event.side in POSITION_SIDES else None
        if side is None and existing is not None and existing.side in POSITION_SIDES:
            # A close whose own snapshot no longer states a side still closes the
            # row we already verified — the last verified side is the truth.
            side = existing.side
        if side is None:
            self.position_writes_skipped += 1
            log.debug(
                "Refusing to write a position with no verified LONG/SHORT side",
                extra={
                    "event": event.event_type.value,
                    "coin": event.coin,
                    "event_side": event.side,
                },
            )
            return

        opened_at = existing.opened_at if existing is not None else None
        if event.event_type is EventType.POSITION_OPENED or opened_at is None:
            opened_at = event.event_time
        max_notional = max(
            abs(position_value or 0.0),
            existing.max_notional if existing is not None else 0.0,
        )
        await PositionRepository.upsert(
            session,
            event.wallet,
            event.coin,
            side=side,
            size=size,
            entry_px=event.numeric("entry_px"),
            position_value=position_value,
            liquidation_px=event.numeric("liquidation_px"),
            leverage=event.numeric("leverage"),
            leverage_type=event.value("leverage_type"),
            margin_used=event.numeric("margin_used"),
            unrealized_pnl=event.numeric("unrealized_pnl"),
            take_profit_px=event.numeric("take_profit_px"),
            stop_loss_px=event.numeric("stop_loss_px"),
            max_notional=max_notional,
            is_open=not closed,
            opened_at=opened_at,
            closed_at=event.event_time if closed else None,
        )

    # ── background loops ──────────────────────────────────────
    async def _universe_loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(self._config_changed.wait(), timeout=UNIVERSE_REFRESH)
                self._config_changed.clear()
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                await self._refresh_universe()
                await self._apply_market_subscriptions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Universe refresh failed")

    async def _position_loop(self) -> None:
        while self._running:
            await asyncio.sleep(POSITION_TICK)
            if not self.config.monitoring_active or not self.config.enable_position_detector:
                continue
            try:
                budget = self.rest.limiter.available
                limit = min(25, int((budget * 0.5) // 2))
                if limit <= 0:
                    continue
                for wallet in self.tracker.due_for_positions(limit):
                    state = await self.rest.clearinghouse_state(
                        wallet.address, wait_for_budget=False
                    )
                    if state is None:
                        self.tracker.record_failure(wallet.address, backoff=60.0)
                        continue
                    await self._ingest_account_state(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Position poll loop failed")

    async def _order_loop(self) -> None:
        while self._running:
            await asyncio.sleep(ORDER_TICK)
            cfg = self.config
            if not cfg.monitoring_active or not cfg.enable_order_detector:
                continue
            try:
                budget = self.rest.limiter.available
                limit = min(4, int((budget * 0.25) // 22))
                if limit <= 0:
                    continue
                order_threshold = cfg.threshold_for("order")
                candidates = [
                    wallet
                    for wallet in self.tracker.due_for_orders(limit * 4)
                    if wallet.pinned
                    or wallet.largest_trade >= order_threshold * DISCOVERY_FACTOR
                    or wallet.total_position_value >= order_threshold
                ][:limit]
                for wallet in candidates:
                    orders = await self.rest.frontend_open_orders(
                        wallet.address, wait_for_budget=False
                    )
                    if orders is None:
                        self.tracker.record_failure(wallet.address, backoff=60.0)
                        continue
                    await self._ingest_open_orders(wallet.address, orders)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Order poll loop failed")

    async def _book_loop(self) -> None:
        """REST fallback for book scanning when the websocket has no l2Book."""
        while self._running:
            await asyncio.sleep(BOOK_TICK)
            cfg = self.config
            if not cfg.monitoring_active or not cfg.enable_book_scanner:
                continue
            if any(
                sub.get("type") == "l2Book" for sub in self.market_ws.subscriptions
            ) and self.market_ws.connected:
                continue
            try:
                for coin in self._universe[:5]:
                    book = await self.rest.l2_book(coin, wait_for_budget=False)
                    if book is not None:
                        await self._process_book(book)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Book scan loop failed")

    async def _focus_loop(self) -> None:
        while self._running:
            await asyncio.sleep(FOCUS_REFRESH)
            try:
                await self._apply_focus_slate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Focus slate update failed")

    async def _apply_focus_slate(self) -> None:
        cfg = self.config
        limit = min(self.env.ws_focus_wallets, MAX_FOCUS_WALLETS)
        wanted: list[str] = []
        if cfg.monitoring_active and cfg.enable_order_detector and limit > 0:
            wanted = self.tracker.focus_slate(limit)

        for address in list(self._user_ws):
            if address not in wanted:
                socket = self._user_ws.pop(address)
                await socket.stop()
                log.info("Focus wallet released", extra={"wallet": address})

        for address in wanted:
            if address in self._user_ws:
                continue
            if len(self._user_ws) >= limit:
                break
            socket = HyperliquidWebSocket(
                self.env.hyperliquid_ws_url,
                lambda channel, data, addr=address: self._on_user_message(addr, channel, data),
                name=f"user:{address[:10]}",
                max_subscriptions=4,
            )
            self._user_ws[address] = socket
            await socket.subscribe({"type": "orderUpdates", "user": address})
            await socket.subscribe({"type": "userEvents", "user": address})
            await socket.start()
            log.info(
                "Focus wallet subscribed",
                extra={"wallet": address, "slate": len(self._user_ws), "cap": limit},
            )

    async def _maintenance_loop(self) -> None:
        while self._running:
            await asyncio.sleep(MAINTENANCE_INTERVAL)
            try:
                pruned = self.tracker.prune()
                purged = self.dedup.purge()
                log.info(
                    "Engine maintenance",
                    extra={
                        "wallets": len(self.tracker),
                        "pruned": pruned,
                        "dedup_purged": purged,
                        "queue": self._queue.qsize(),
                        "alerts": self.events_alerted,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Maintenance loop failed")

    # ── observability ─────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self.market_ws.connected

    @property
    def unique_ws_users(self) -> int:
        return len({address for address in self._user_ws})

    @property
    def known_coins(self) -> tuple[str, ...]:
        """Hyperliquid's perpetual universe as last reported by ``meta``.

        Empty until the first successful REST call — callers fall back to their
        own suggestions rather than pretending a coin exists.
        """
        return tuple(sorted(self._known_coins))

    @property
    def monitored_coins(self) -> tuple[str, ...]:
        """Coins actually subscribed right now."""
        return self._universe

    @property
    def unmonitored_count(self) -> int:
        """Coins excluded by MAX_MONITORED_COINS or unknown to Hyperliquid."""
        return len(self._dropped_coins)

    def stats(self) -> dict[str, Any]:
        uptime = (utc_now() - self.started_at).total_seconds() if self.started_at else 0.0
        return {
            "running": self._running,
            "uptime_seconds": round(uptime, 1),
            "monitoring_enabled": self.config.monitoring_enabled,
            "paused": self.config.paused,
            "coins_monitored": list(self._universe),
            "coins_not_monitored": len(self._dropped_coins),
            "trades_seen": self.trades_seen,
            "candidates": self.candidates,
            "events_detected": self.events_detected,
            "events_alerted": self.events_alerted,
            "queue_depth": self._queue.qsize(),
            "queue_dropped": self.queue_dropped,
            "persist_errors": self.persist_errors,
            "duplicate_persisted": self.duplicate_persisted,
            "position_writes_skipped": self.position_writes_skipped,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "last_alert_at": self.last_alert_at.isoformat() if self.last_alert_at else None,
            "market_ws": self.market_ws.stats(),
            "focus_wallets": sorted(self._user_ws),
            "focus_cap": min(self.env.ws_focus_wallets, MAX_FOCUS_WALLETS),
            "rest": self.rest.stats(),
            "tracker": self.tracker.stats(),
            "filter": self.filter.stats.as_dict(),
            "dedup": self.dedup.as_dict(),
        }
