"""Runtime configuration service.

Environment variables are *bootstrap* values: on first boot they seed the
``settings`` table and the ``tracked_coins`` table. From then on the database is
authoritative, so every change an administrator makes through Telegram survives
a Railway restart or redeploy (spec §48/§49).

Consumers hold a reference to :class:`SettingsService` and read
``service.config`` — an immutable-ish snapshot that is swapped atomically on
change. Listeners registered with :meth:`SettingsService.on_change` are awaited
after a change so the ingest engine can resubscribe feeds.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Sequence

from app.config import Settings
from app.database.base import Database
from app.database.repository import (
    AuditRepository,
    CoinRepository,
    SettingsRepository,
    WalletRepository,
)
from app.hyperliquid.constants import DEFAULT_WINDOW, MONITOR_WINDOWS
from app.utils.logging import get_logger

log = get_logger(__name__)

# ── setting keys (also the audit action targets) ───────────────
KEY_MONITORING = "monitoring_enabled"
#: The global pause. Distinct from ``monitoring_enabled`` on purpose: pausing
#: must not destroy the monitoring setting an admin chose, so /go restores
#: exactly the state that was in force before /pause.
KEY_PAUSED = "paused"
KEY_PUBLIC_MODE = "public_mode"
KEY_MIN_WHALE = "min_whale_value"
KEY_MIN_TRADE = "min_trade_value"
KEY_MIN_POSITION = "min_position_value"
KEY_MIN_POSITION_DELTA = "min_position_delta_value"
KEY_MIN_ORDER = "min_order_value"
KEY_MIN_MARGIN = "min_margin_value"
KEY_COOLDOWN = "alert_cooldown_seconds"
KEY_ALL_COINS = "all_coins"
KEY_MAX_COINS = "max_monitored_coins"
KEY_TRADES = "enable_trade_detector"
KEY_POSITIONS = "enable_position_detector"
KEY_ORDERS = "enable_order_detector"
KEY_CANCELS = "enable_order_cancel_alerts"
KEY_WALLETS = "enable_wallet_tracking"
KEY_BOOK = "enable_book_scanner"
KEY_WINDOW = "default_window"

BOOL_KEYS = frozenset(
    {
        KEY_MONITORING,
        KEY_PAUSED,
        KEY_PUBLIC_MODE,
        KEY_ALL_COINS,
        KEY_TRADES,
        KEY_POSITIONS,
        KEY_ORDERS,
        KEY_CANCELS,
        KEY_WALLETS,
        KEY_BOOK,
    }
)

MIN_THRESHOLD = 1_000.0
MAX_THRESHOLD = 1_000_000_000.0
MIN_COOLDOWN = 0
MAX_COOLDOWN = 3_600

#: ``0`` means the margin gate is off. Any positive value is clamped into the
#: same band as the notional thresholds.
MARGIN_OFF = 0.0


@dataclass(frozen=True)
class RuntimeConfig:
    monitoring_enabled: bool = True
    #: Global pause. When true the bot does nothing at all: no market feeds, no
    #: detection, no alerts, and every command except /go, /status and the
    #: always-available basics is refused. Stored in the database, so a pause
    #: survives a redeploy.
    paused: bool = False
    public_mode: bool = False

    min_whale_value: float = 2_000_000.0
    min_trade_value: float | None = None
    min_position_value: float | None = None
    min_position_delta_value: float | None = None
    min_order_value: float | None = None

    #: Margin gate. Hyperliquid reports ``marginUsed`` per position, so this is
    #: a *second, independent* gate that applies to events carrying a position:
    #: the collateral actually at risk, never the position notional. ``0``
    #: disables it, which is the default so existing deployments do not
    #: suddenly go quiet.
    min_margin_value: float = 0.0

    alert_cooldown_seconds: int = 30

    #: Empty tuple means "ALL COINS" is in effect.
    coins: tuple[str, ...] = ()
    all_coins: bool = False
    max_monitored_coins: int = 40

    enable_trade_detector: bool = True
    enable_position_detector: bool = True
    enable_order_detector: bool = True
    enable_order_cancel_alerts: bool = True
    enable_wallet_tracking: bool = True
    enable_book_scanner: bool = False

    default_window: str = DEFAULT_WINDOW
    tracked_wallets: tuple[str, ...] = field(default=())

    @property
    def monitoring_active(self) -> bool:
        """Whether the ingest pipeline should run at all.

        Every gate in :mod:`app.whale.engine` reads this rather than
        ``monitoring_enabled``, so the global pause cannot be bypassed by a code
        path that forgot about it.
        """
        return self.monitoring_enabled and not self.paused

    # ── thresholds ────────────────────────────────────────────
    def threshold_for(self, threshold_class: str) -> float:
        override = {
            "trade": self.min_trade_value,
            "position": self.min_position_value,
            "position_delta": self.min_position_delta_value,
            "order": self.min_order_value,
        }.get(threshold_class)
        return float(override) if override else float(self.min_whale_value)

    @property
    def effective_thresholds(self) -> dict[str, float]:
        return {
            "trade": self.threshold_for("trade"),
            "position": self.threshold_for("position"),
            "position_delta": self.threshold_for("position_delta"),
            "order": self.threshold_for("order"),
        }

    @property
    def lowest_threshold(self) -> float:
        """The cheapest gate any detector applies — used for prefiltering."""
        return min(self.effective_thresholds.values())

    @property
    def margin_gate_enabled(self) -> bool:
        return self.min_margin_value > 0

    # ── coins ─────────────────────────────────────────────────
    def coin_enabled(self, coin: str) -> bool:
        if self.all_coins:
            return True
        return coin.upper() in self.coins

    @property
    def coin_label(self) -> str:
        if self.all_coins:
            return "ALL COINS"
        return ", ".join(self.coins) if self.coins else "none selected"

    def detector_enabled(self, name: str) -> bool:
        return {
            "trade": self.enable_trade_detector,
            "position": self.enable_position_detector,
            "order": self.enable_order_detector,
            "book": self.enable_book_scanner,
        }.get(name, True)


ChangeListener = Callable[[RuntimeConfig], Awaitable[None]]


class SettingsService:
    def __init__(self, database: Database, env: Settings) -> None:
        self.db = database
        self.env = env
        self._config = RuntimeConfig()
        self._listeners: list[ChangeListener] = []
        self._lock = asyncio.Lock()
        self.loaded = False

    # ── access ────────────────────────────────────────────────
    @property
    def config(self) -> RuntimeConfig:
        return self._config

    def on_change(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    async def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                await listener(self._config)
            except Exception:  # a bad listener must not break the setter
                log.exception("Settings change listener failed")

    # ── (de)serialisation ─────────────────────────────────────
    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value)

    @staticmethod
    def _decode(raw: str | None, default: Any) -> Any:
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    # ── bootstrap ─────────────────────────────────────────────
    async def load(self) -> RuntimeConfig:
        """Read settings from the database, seeding from env on first boot."""
        env = self.env
        defaults: dict[str, Any] = {
            KEY_MONITORING: True,
            KEY_PAUSED: False,
            KEY_PUBLIC_MODE: env.public_mode,
            KEY_MIN_WHALE: float(env.min_whale_value),
            KEY_MIN_TRADE: env.min_trade_value,
            KEY_MIN_POSITION: env.min_position_value,
            KEY_MIN_POSITION_DELTA: env.min_position_delta_value,
            KEY_MIN_ORDER: env.min_order_value,
            KEY_MIN_MARGIN: float(env.min_margin_value or 0.0),
            KEY_COOLDOWN: int(env.alert_cooldown_seconds),
            KEY_ALL_COINS: bool(env.monitor_all_coins),
            KEY_MAX_COINS: int(env.max_monitored_coins),
            KEY_TRADES: env.enable_trade_detector,
            KEY_POSITIONS: env.enable_position_detector,
            KEY_ORDERS: env.enable_order_detector,
            KEY_CANCELS: env.enable_order_cancel_alerts,
            KEY_WALLETS: env.enable_wallet_tracking,
            KEY_BOOK: env.enable_book_scanner,
            KEY_WINDOW: DEFAULT_WINDOW,
        }

        async with self.db.session() as session:
            stored = await SettingsRepository.all(session)
            missing = {k: v for k, v in defaults.items() if k not in stored}
            for key, value in missing.items():
                await SettingsRepository.set(session, key, self._encode(value))
            if missing:
                log.info("Seeded settings from environment", extra={"keys": sorted(missing)})

            coins = await CoinRepository.enabled(session)
            if not coins and not stored:
                # First boot: seed the coin list from DEFAULT_COINS.
                seed = env.default_coin_list
                await CoinRepository.replace(session, seed)
                coins = list(seed)
                log.info("Seeded tracked coins from environment", extra={"coins": coins})

            merged = {**defaults, **{k: self._decode(v, defaults.get(k)) for k, v in stored.items()}}
            wallets = [row.address for row in await WalletRepository.tracked(session)]

        self._config = self._build(merged, coins, wallets)
        self.loaded = True
        log.info(
            "Runtime configuration loaded",
            extra={
                "monitoring": self._config.monitoring_enabled,
                "public_mode": self._config.public_mode,
                "threshold": self._config.min_whale_value,
                "coins": self._config.coin_label,
            },
        )
        await self._notify()
        return self._config

    def _build(
        self, values: dict[str, Any], coins: Sequence[str], wallets: Sequence[str]
    ) -> RuntimeConfig:
        def as_bool(key: str, default: bool) -> bool:
            raw = values.get(key, default)
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw)

        def as_float(key: str) -> float | None:
            raw = values.get(key)
            try:
                return float(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        def as_int(key: str, default: int) -> int:
            try:
                return int(values.get(key, default))
            except (TypeError, ValueError):
                return default

        window = str(values.get(KEY_WINDOW, DEFAULT_WINDOW)).upper()
        if window not in MONITOR_WINDOWS:
            window = DEFAULT_WINDOW

        return RuntimeConfig(
            monitoring_enabled=as_bool(KEY_MONITORING, True),
            paused=as_bool(KEY_PAUSED, False),
            public_mode=as_bool(KEY_PUBLIC_MODE, False),
            min_whale_value=as_float(KEY_MIN_WHALE) or float(self.env.min_whale_value),
            min_trade_value=as_float(KEY_MIN_TRADE),
            min_position_value=as_float(KEY_MIN_POSITION),
            min_position_delta_value=as_float(KEY_MIN_POSITION_DELTA),
            min_order_value=as_float(KEY_MIN_ORDER),
            min_margin_value=as_float(KEY_MIN_MARGIN) or 0.0,
            alert_cooldown_seconds=as_int(KEY_COOLDOWN, 30),
            coins=tuple(sorted({c.upper() for c in coins})),
            all_coins=as_bool(KEY_ALL_COINS, False),
            max_monitored_coins=as_int(KEY_MAX_COINS, 40),
            enable_trade_detector=as_bool(KEY_TRADES, True),
            enable_position_detector=as_bool(KEY_POSITIONS, True),
            enable_order_detector=as_bool(KEY_ORDERS, True),
            enable_order_cancel_alerts=as_bool(KEY_CANCELS, True),
            enable_wallet_tracking=as_bool(KEY_WALLETS, True),
            enable_book_scanner=as_bool(KEY_BOOK, False),
            default_window=window,
            tracked_wallets=tuple(w.lower() for w in wallets),
        )

    # ── mutation ──────────────────────────────────────────────
    async def set_value(
        self, key: str, value: Any, admin_id: int | None = None, *, audit: bool = True
    ) -> RuntimeConfig:
        """Persist one setting, refresh the snapshot, audit, then notify."""
        async with self._lock:
            old = getattr(self._config, key, None)
            async with self.db.session() as session:
                await SettingsRepository.set(session, key, self._encode(value), admin_id)
                if audit and admin_id is not None:
                    await AuditRepository.record(session, admin_id, f"set:{key}", key, old, value)
            self._config = replace(self._config, **{key: value})
        log.info(
            "Setting changed",
            extra={"key": key, "old": old, "new": value, "admin": admin_id},
        )
        await self._notify()
        return self._config

    async def set_threshold(
        self, value: float, admin_id: int | None = None, key: str = KEY_MIN_WHALE
    ) -> float:
        clamped = max(MIN_THRESHOLD, min(float(value), MAX_THRESHOLD))
        await self.set_value(key, clamped, admin_id)
        return clamped

    async def set_margin(self, value: float, admin_id: int | None = None) -> float:
        """Set the minimum ``marginUsed`` a position must carry to alert.

        ``0`` (or anything negative) turns the gate off. This is deliberately a
        separate setter from :meth:`set_threshold`: margin is collateral at
        risk, the thresholds are notional value, and conflating the two would
        silently change what every existing filter means.
        """
        value = float(value)
        clamped = MARGIN_OFF if value <= 0 else max(MIN_THRESHOLD, min(value, MAX_THRESHOLD))
        await self.set_value(KEY_MIN_MARGIN, clamped, admin_id)
        return clamped

    async def set_cooldown(self, seconds: int, admin_id: int | None = None) -> int:
        clamped = max(MIN_COOLDOWN, min(int(seconds), MAX_COOLDOWN))
        await self.set_value(KEY_COOLDOWN, clamped, admin_id)
        return clamped

    async def set_monitoring(self, enabled: bool, admin_id: int | None = None) -> None:
        await self.set_value(KEY_MONITORING, bool(enabled), admin_id)

    async def set_paused(self, paused: bool, admin_id: int | None = None) -> None:
        """The global pause (/pause and /go).

        Written to the database like any other setting, so it is audited and it
        survives a restart: a paused bot comes back paused rather than silently
        resuming after a redeploy.
        """
        await self.set_value(KEY_PAUSED, bool(paused), admin_id)

    async def set_public_mode(self, enabled: bool, admin_id: int | None = None) -> None:
        await self.set_value(KEY_PUBLIC_MODE, bool(enabled), admin_id)

    async def toggle(self, key: str, admin_id: int | None = None) -> bool:
        if key not in BOOL_KEYS:
            raise ValueError(f"{key} is not a boolean setting")
        new_value = not bool(getattr(self._config, key, False))
        await self.set_value(key, new_value, admin_id)
        return new_value

    async def set_window(self, window: str, admin_id: int | None = None) -> str:
        window = window.upper()
        if window not in MONITOR_WINDOWS:
            window = DEFAULT_WINDOW
        await self.set_value(KEY_WINDOW, window, admin_id)
        return window

    # ── coins ─────────────────────────────────────────────────
    async def _refresh_coins(self) -> None:
        async with self.db.session() as session:
            coins = await CoinRepository.enabled(session)
        async with self._lock:
            self._config = replace(self._config, coins=tuple(sorted({c.upper() for c in coins})))
        await self._notify()

    async def set_coins(self, coins: Sequence[str], admin_id: int | None = None) -> tuple[str, ...]:
        cleaned = [c.strip().upper() for c in coins if c.strip()]
        async with self.db.session() as session:
            old = await CoinRepository.enabled(session)
            await CoinRepository.replace(session, cleaned, admin_id)
            if admin_id is not None:
                await AuditRepository.record(
                    session, admin_id, "set:coins", "tracked_coins", ",".join(old), ",".join(cleaned)
                )
        await self._refresh_coins()
        return self._config.coins

    async def add_coin(self, coin: str, admin_id: int | None = None) -> bool:
        coin = coin.strip().upper()
        async with self.db.session() as session:
            added = await CoinRepository.add(session, coin, admin_id)
            if added and admin_id is not None:
                await AuditRepository.record(session, admin_id, "add:coin", coin, None, coin)
        if added:
            await self._refresh_coins()
        return added

    async def remove_coin(self, coin: str, admin_id: int | None = None) -> bool:
        coin = coin.strip().upper()
        async with self.db.session() as session:
            removed = await CoinRepository.remove(session, coin)
            if removed and admin_id is not None:
                await AuditRepository.record(session, admin_id, "remove:coin", coin, coin, None)
        if removed:
            await self._refresh_coins()
        return removed

    # ── tracked wallets ───────────────────────────────────────
    async def add_tracked_wallet(
        self, address: str, admin_id: int | None = None, label: str | None = None
    ) -> bool:
        address = address.strip().lower()
        async with self.db.session() as session:
            added = await WalletRepository.add_tracked(session, address, admin_id, label)
            if added and admin_id is not None:
                await AuditRepository.record(session, admin_id, "watch:wallet", address, None, address)
            wallets = [row.address for row in await WalletRepository.tracked(session)]
        async with self._lock:
            self._config = replace(self._config, tracked_wallets=tuple(wallets))
        await self._notify()
        return added

    async def remove_tracked_wallet(self, address: str, admin_id: int | None = None) -> bool:
        address = address.strip().lower()
        async with self.db.session() as session:
            removed = await WalletRepository.remove_tracked(session, address)
            if removed and admin_id is not None:
                await AuditRepository.record(
                    session, admin_id, "unwatch:wallet", address, address, None
                )
            wallets = [row.address for row in await WalletRepository.tracked(session)]
        async with self._lock:
            self._config = replace(self._config, tracked_wallets=tuple(wallets))
        await self._notify()
        return removed
