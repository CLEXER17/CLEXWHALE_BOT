"""Wallet state tracking.

Hyperliquid gives us wallet addresses for free on the global ``trades`` feed,
but everything *about* those wallets (positions, resting orders, TP/SL) costs
either REST weight or one of the ten unique-address websocket slots. This module
is the bookkeeping that makes those budgets go as far as possible:

* remember which wallets have shown whale-sized activity, with a decaying score
  so today's whale outranks last week's;
* keep the last observed position and order state per wallet so the detectors
  have something to diff against;
* elect a small **focus slate** for per-wallet websocket subscriptions
  (``orderUpdates``), respecting the documented cap of 10 unique addresses;
* schedule REST polls so a fixed weight budget covers as many wallets as
  possible, newest-and-largest first.

Nothing here talks to the network. The engine owns the I/O and calls in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.hyperliquid.models import OpenOrder, Position
from app.utils.formatting import utc_now
from app.utils.logging import get_logger
from app.whale.detector import OrderState, PositionContext

log = get_logger(__name__)

#: A wallet's activity score halves over this many seconds of silence.
SCORE_HALF_LIFE = 6 * 3600.0
#: Score bonus that keeps admin-watched wallets in the focus slate.
PINNED_BONUS = 1e15


def order_state_from_open_order(order: OpenOrder) -> OrderState:
    return OrderState(
        oid=order.oid,
        coin=order.coin,
        side=order.side,
        limit_px=order.price,
        size=order.sz,
        orig_size=order.orig_sz,
        notional=order.notional,
        status="open",
        is_trigger=order.is_trigger,
        trigger_px=order.trigger_px,
        order_type=order.order_type,
        reduce_only=order.reduce_only,
        placed_at=order.timestamp,
    )


@dataclass(slots=True)
class TrackedWallet:
    address: str
    first_seen: datetime
    last_seen: datetime
    #: Pinned wallets come from the admin ``/watch`` list and are never evicted.
    pinned: bool = False

    trade_count: int = 0
    trade_volume: float = 0.0
    largest_trade: float = 0.0
    alert_count: int = 0
    coins: dict[str, float] = field(default_factory=dict)

    _score: float = 0.0
    _scored_at: datetime | None = None

    #: Last observed ``clearinghouseState`` positions, keyed by coin.
    positions: dict[str, Position] = field(default_factory=dict)
    positions_at: datetime | None = None
    account_value: float | None = None
    #: When this bot first saw each position — not the on-chain open time.
    position_first_seen: dict[str, datetime] = field(default_factory=dict)

    #: Last observed resting orders, keyed by oid.
    orders: dict[int, OrderState] = field(default_factory=dict)
    orders_at: datetime | None = None

    next_position_poll: datetime | None = None
    next_order_poll: datetime | None = None
    #: Consecutive failed enrichment attempts; used to back off dead wallets.
    failures: int = 0

    # ── scoring ───────────────────────────────────────────────
    def score(self, now: datetime | None = None) -> float:
        now = now or utc_now()
        base = self._decayed(now)
        return base + (PINNED_BONUS if self.pinned else 0.0)

    def _decayed(self, now: datetime) -> float:
        if self._scored_at is None:
            return self._score
        elapsed = (now - self._scored_at).total_seconds()
        if elapsed <= 0:
            return self._score
        return self._score * math.pow(0.5, elapsed / SCORE_HALF_LIFE)

    def add_score(self, amount: float, now: datetime) -> None:
        self._score = self._decayed(now) + max(0.0, amount)
        self._scored_at = now

    # ── derived views ─────────────────────────────────────────
    @property
    def total_position_value(self) -> float:
        return sum(position.notional for position in self.positions.values())

    @property
    def open_coins(self) -> tuple[str, ...]:
        return tuple(self.positions)

    @property
    def has_baseline(self) -> bool:
        """True once we have fetched positions at least once, which is what
        makes a later diff meaningful rather than a guess about history."""
        return self.positions_at is not None

    def idle_seconds(self, now: datetime | None = None) -> float:
        return ((now or utc_now()) - self.last_seen).total_seconds()

    def as_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "pinned": self.pinned,
            "trades": self.trade_count,
            "volume": self.trade_volume,
            "largest_trade": self.largest_trade,
            "alerts": self.alert_count,
            "coins": sorted(self.coins, key=lambda c: self.coins[c], reverse=True),
            "position_value": self.total_position_value,
            "score": round(self.score(), 2),
            "last_seen": self.last_seen.isoformat(),
        }


class WalletTracker:
    """Bounded, self-pruning registry of interesting wallets."""

    def __init__(
        self,
        max_wallets: int = 400,
        idle_ttl: float = 3600.0,
        position_interval: float = 20.0,
        order_interval: float = 45.0,
    ) -> None:
        self.max_wallets = max(8, max_wallets)
        self.idle_ttl = idle_ttl
        self.position_interval = position_interval
        self.order_interval = order_interval
        self._wallets: dict[str, TrackedWallet] = {}
        self.evicted = 0

    # ── container protocol ────────────────────────────────────
    def __len__(self) -> int:
        return len(self._wallets)

    def __contains__(self, address: str) -> bool:
        return address.lower() in self._wallets

    def get(self, address: str) -> TrackedWallet | None:
        return self._wallets.get(address.lower())

    def all(self) -> list[TrackedWallet]:
        return list(self._wallets.values())

    def top(self, limit: int = 10, now: datetime | None = None) -> list[TrackedWallet]:
        now = now or utc_now()
        return sorted(self._wallets.values(), key=lambda w: w.score(now), reverse=True)[:limit]

    # ── ingestion ─────────────────────────────────────────────
    def touch(
        self, address: str, when: datetime | None = None, *, pinned: bool = False
    ) -> TrackedWallet:
        key = address.lower()
        now = when or utc_now()
        wallet = self._wallets.get(key)
        if wallet is None:
            wallet = TrackedWallet(address=key, first_seen=now, last_seen=now, pinned=pinned)
            self._wallets[key] = wallet
            self._enforce_capacity(now)
        else:
            if now > wallet.last_seen:
                wallet.last_seen = now
            if pinned:
                wallet.pinned = True
        return wallet

    def observe_trade(
        self, address: str, coin: str, notional: float, when: datetime | None = None
    ) -> TrackedWallet:
        """Record a whale-sized trade. The score is what drives focus-slate
        election and REST poll ordering."""
        now = when or utc_now()
        wallet = self.touch(address, now)
        wallet.trade_count += 1
        wallet.trade_volume += abs(notional)
        wallet.largest_trade = max(wallet.largest_trade, abs(notional))
        wallet.coins[coin] = wallet.coins.get(coin, 0.0) + abs(notional)
        wallet.add_score(abs(notional), now)
        return wallet

    def record_alert(self, address: str | None) -> None:
        if not address:
            return
        wallet = self.get(address)
        if wallet is not None:
            wallet.alert_count += 1

    def pin(self, addresses: list[str] | tuple[str, ...]) -> None:
        """Apply the admin watch list. Pinned wallets are always enriched."""
        wanted = {a.lower() for a in addresses}
        for address in wanted:
            self.touch(address, pinned=True)
        for wallet in self._wallets.values():
            wallet.pinned = wallet.address in wanted

    # ── state application ─────────────────────────────────────
    def apply_positions(
        self,
        address: str,
        positions: dict[str, Position],
        *,
        account_value: float | None = None,
        when: datetime | None = None,
    ) -> tuple[dict[str, Position] | None, dict[str, Position]]:
        """Store a fresh ``clearinghouseState`` snapshot.

        Returns ``(previous, current)``. ``previous`` is ``None`` on the first
        snapshot for a wallet, which tells the engine to record a baseline
        instead of announcing pre-existing positions as if they were new.
        """
        now = when or utc_now()
        wallet = self.touch(address, now)
        previous = dict(wallet.positions) if wallet.has_baseline else None

        for coin in positions:
            wallet.position_first_seen.setdefault(coin, now)
        for coin in list(wallet.position_first_seen):
            if coin not in positions:
                wallet.position_first_seen.pop(coin, None)

        wallet.positions = dict(positions)
        wallet.positions_at = now
        if account_value is not None:
            wallet.account_value = account_value
        wallet.failures = 0
        wallet.next_position_poll = now + timedelta(seconds=self.position_interval)
        return previous, wallet.positions

    def apply_orders(
        self,
        address: str,
        orders: list[OpenOrder],
        *,
        when: datetime | None = None,
    ) -> tuple[dict[int, OrderState] | None, dict[int, OrderState]]:
        """Store a fresh ``frontendOpenOrders`` snapshot, returning the previous
        one (``None`` on first observation) for diffing."""
        now = when or utc_now()
        wallet = self.touch(address, now)
        previous = dict(wallet.orders) if wallet.orders_at is not None else None
        wallet.orders = {order.oid: order_state_from_open_order(order) for order in orders}
        wallet.orders_at = now
        wallet.failures = 0
        wallet.next_order_poll = now + timedelta(seconds=self.order_interval)
        return previous, wallet.orders

    def apply_order_state(self, address: str, state: OrderState) -> OrderState | None:
        """Apply a single live ``orderUpdates`` frame, returning the prior state."""
        wallet = self.touch(address)
        previous = wallet.orders.get(state.oid)
        if state.status == "open" and state.size > 0:
            wallet.orders[state.oid] = state
        else:
            wallet.orders.pop(state.oid, None)
        if wallet.orders_at is None:
            wallet.orders_at = utc_now()
        return previous

    def record_failure(self, address: str, *, backoff: float = 120.0) -> None:
        """Push a wallet's next poll out after a failed or empty enrichment."""
        wallet = self.get(address)
        if wallet is None:
            return
        wallet.failures += 1
        delay = min(backoff * wallet.failures, 900.0)
        now = utc_now()
        wallet.next_position_poll = now + timedelta(seconds=delay)
        wallet.next_order_poll = now + timedelta(seconds=delay)

    def defer(self, address: str, seconds: float) -> None:
        wallet = self.get(address)
        if wallet is None:
            return
        when = utc_now() + timedelta(seconds=seconds)
        wallet.next_position_poll = when
        wallet.next_order_poll = when

    # ── context for detectors ─────────────────────────────────
    def context_for(self, address: str, coin: str) -> PositionContext:
        wallet = self.get(address)
        if wallet is None:
            return PositionContext()
        trigger_orders = [
            OpenOrder(
                coin=state.coin,
                oid=state.oid,
                side=state.side,
                limit_px=state.limit_px,
                sz=state.size,
                orig_sz=state.orig_size,
                timestamp=state.placed_at,
                order_type=state.order_type,
                reduce_only=state.reduce_only,
                is_trigger=state.is_trigger,
                trigger_px=state.trigger_px,
            )
            for state in wallet.orders.values()
            if state.is_trigger and state.coin == coin
        ]
        return PositionContext(
            position=wallet.positions.get(coin),
            trigger_orders=trigger_orders,
            account_value=wallet.account_value,
            first_seen=wallet.position_first_seen.get(coin),
            orders_known=wallet.orders_at is not None,
        )

    # ── scheduling ────────────────────────────────────────────
    def due_for_positions(self, limit: int, now: datetime | None = None) -> list[TrackedWallet]:
        now = now or utc_now()
        due = [
            wallet
            for wallet in self._wallets.values()
            if wallet.next_position_poll is None or wallet.next_position_poll <= now
        ]
        due.sort(key=lambda w: w.score(now), reverse=True)
        return due[: max(0, limit)]

    def due_for_orders(self, limit: int, now: datetime | None = None) -> list[TrackedWallet]:
        now = now or utc_now()
        due = [
            wallet
            for wallet in self._wallets.values()
            if wallet.next_order_poll is None or wallet.next_order_poll <= now
        ]
        due.sort(key=lambda w: w.score(now), reverse=True)
        return due[: max(0, limit)]

    def focus_slate(self, limit: int, now: datetime | None = None) -> list[str]:
        """Addresses that deserve a per-wallet websocket subscription.

        Hyperliquid allows 10 unique addresses across *all* user subscriptions
        per IP, so ``limit`` is expected to be small; pinned wallets win.
        """
        now = now or utc_now()
        ranked = sorted(self._wallets.values(), key=lambda w: w.score(now), reverse=True)
        return [wallet.address for wallet in ranked[: max(0, limit)]]

    # ── maintenance ───────────────────────────────────────────
    def prune(self, now: datetime | None = None) -> int:
        """Drop wallets that have been quiet for longer than the idle TTL."""
        now = now or utc_now()
        stale = [
            address
            for address, wallet in self._wallets.items()
            if not wallet.pinned
            and not wallet.positions
            and wallet.idle_seconds(now) > self.idle_ttl
        ]
        for address in stale:
            self._wallets.pop(address, None)
        return len(stale)

    def _enforce_capacity(self, now: datetime) -> None:
        overflow = len(self._wallets) - self.max_wallets
        if overflow <= 0:
            return
        # Evict the lowest-scoring unpinned wallets; pinned ones never go.
        candidates = sorted(
            (w for w in self._wallets.values() if not w.pinned),
            key=lambda w: w.score(now),
        )
        for wallet in candidates[:overflow]:
            self._wallets.pop(wallet.address, None)
            self.evicted += 1

    def stats(self) -> dict[str, object]:
        now = utc_now()
        with_positions = sum(1 for w in self._wallets.values() if w.positions)
        return {
            "tracked": len(self._wallets),
            "pinned": sum(1 for w in self._wallets.values() if w.pinned),
            "with_positions": with_positions,
            "with_orders": sum(1 for w in self._wallets.values() if w.orders),
            "baselined": sum(1 for w in self._wallets.values() if w.has_baseline),
            "evicted": self.evicted,
            "capacity": self.max_wallets,
            "top": [w.as_dict() for w in self.top(5, now)],
        }
