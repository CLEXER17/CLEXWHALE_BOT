"""Hyperliquid API constants, verified against the official documentation.

Sources
-------
* Info endpoint / perpetuals ....... POST {api}/info
* Websocket subscriptions ......... wss://api.hyperliquid.xyz/ws
* Rate limits ..................... 1200 aggregated REST weight / minute / IP

Documented limits that materially shape this project's architecture:

* ``trades`` carries ``users: [buyer, seller]`` — the only *global* feed that
  attributes activity to addresses. It is our whale-discovery backbone.
* User-specific websocket subscriptions are capped at **10 unique addresses
  per IP**, so per-wallet streams can only cover a small focus slate; wider
  coverage must go through weight-budgeted REST polling.
* ``l2Book`` returns at most 20 levels per side and carries **no** wallet
  attribution — only an aggregate order count ``n`` per price level.
"""

from __future__ import annotations

from enum import Enum

INFO_PATH = "/info"
DEFAULT_API_URL = "https://api.hyperliquid.xyz"
DEFAULT_WS_URL = "wss://api.hyperliquid.xyz/ws"

# ── REST weights ───────────────────────────────────────────────
CHEAP_INFO_REQUESTS = frozenset(
    {
        "l2Book",
        "allMids",
        "clearinghouseState",
        "orderStatus",
        "spotClearinghouseState",
        "exchangeStatus",
    }
)
WEIGHT_CHEAP = 2
WEIGHT_DEFAULT = 20
WEIGHT_USER_ROLE = 60


def info_weight(request_type: str) -> int:
    if request_type == "userRole":
        return WEIGHT_USER_ROLE
    if request_type in CHEAP_INFO_REQUESTS:
        return WEIGHT_CHEAP
    return WEIGHT_DEFAULT


# ── Websocket ──────────────────────────────────────────────────
#: Subscriptions that take a ``user`` field and therefore consume one of the
#: 10 unique-address slots.
USER_SUBSCRIPTIONS = frozenset(
    {
        "notification",
        "webData2",
        "webData3",
        "orderUpdates",
        "userEvents",
        "userFills",
        "userFundings",
        "userNonFundingLedgerUpdates",
        "activeAssetData",
        "userTwapSliceFills",
        "userTwapHistory",
        "clearinghouseState",
        "openOrders",
        "twapStates",
        "spotState",
        "allDexsClearinghouseState",
    }
)

MAX_WS_CONNECTIONS = 10
MAX_WS_SUBSCRIPTIONS = 1000
MAX_WS_UNIQUE_USERS = 10
MAX_WS_MESSAGES_PER_MINUTE = 2000

#: ``userEvents`` arrives on channel ``user``, not ``userEvents``.
CHANNEL_ALIASES = {"user": "userEvents"}


# ── Trade side encoding ────────────────────────────────────────
#: In ``trades`` / ``userFills``, ``side`` is the *taker* side.
SIDE_BUY = "B"
SIDE_SELL = "A"


def side_label(raw: str | None) -> str | None:
    if raw == SIDE_BUY:
        return "BUY"
    if raw == SIDE_SELL:
        return "SELL"
    return None


# ── Order statuses (complete list from the info endpoint docs) ──
STATUS_OPEN = "open"
STATUS_FILLED = "filled"
STATUS_CANCELED = "canceled"
STATUS_TRIGGERED = "triggered"

ALL_ORDER_STATUSES = (
    "open",
    "filled",
    "canceled",
    "triggered",
    "rejected",
    "marginCanceled",
    "vaultWithdrawalCanceled",
    "openInterestCapCanceled",
    "selfTradeCanceled",
    "reduceOnlyCanceled",
    "siblingFilledCanceled",
    "delistedCanceled",
    "liquidatedCanceled",
    "scheduledCancel",
    "tickRejected",
    "minTradeNtlRejected",
    "perpMarginRejected",
    "reduceOnlyRejected",
    "badAloPxRejected",
    "iocCancelRejected",
    "badTriggerPxRejected",
    "marketOrderNoLiquidityRejected",
    "positionIncreaseAtOpenInterestCapRejected",
    "positionFlipAtOpenInterestCapRejected",
    "tooAggressiveAtOpenInterestCapRejected",
    "openInterestIncreaseRejected",
    "insufficientSpotBalanceRejected",
    "oracleRejected",
    "perpMaxPositionRejected",
)

CANCEL_STATUSES = frozenset(
    s for s in ALL_ORDER_STATUSES if s.endswith("Canceled") or s == "canceled"
) | {"scheduledCancel"}
REJECT_STATUSES = frozenset(s for s in ALL_ORDER_STATUSES if s.endswith("Rejected") or s == "rejected")

#: Human labels for the statuses an operator actually wants to read.
STATUS_LABELS = {
    "open": "OPEN",
    "filled": "FILLED",
    "canceled": "CANCELLED BY TRADER",
    "triggered": "TRIGGERED",
    "rejected": "REJECTED",
    "marginCanceled": "CANCELLED (insufficient margin)",
    "vaultWithdrawalCanceled": "CANCELLED (vault withdrawal)",
    "openInterestCapCanceled": "CANCELLED (open-interest cap)",
    "selfTradeCanceled": "CANCELLED (self-trade)",
    "reduceOnlyCanceled": "CANCELLED (reduce-only)",
    "siblingFilledCanceled": "CANCELLED (sibling TP/SL filled)",
    "delistedCanceled": "CANCELLED (asset delisted)",
    "liquidatedCanceled": "CANCELLED (liquidation)",
    "scheduledCancel": "CANCELLED (dead-man switch)",
}


def status_label(status: str | None) -> str:
    if not status:
        return "UNKNOWN"
    return STATUS_LABELS.get(status, status)


def is_cancel_status(status: str | None) -> bool:
    return bool(status) and status in CANCEL_STATUSES


def is_reject_status(status: str | None) -> bool:
    return bool(status) and status in REJECT_STATUSES


# ── Trigger order types (source of real TP / SL levels) ────────
#: ``frontendOpenOrders`` exposes ``orderType`` strings like these.
TAKE_PROFIT_TYPES = frozenset({"Take Profit Market", "Take Profit Limit"})
STOP_LOSS_TYPES = frozenset({"Stop Market", "Stop Limit"})


class TriggerKind(str, Enum):
    TAKE_PROFIT = "TP"
    STOP_LOSS = "SL"
    OTHER = "TRIGGER"


def classify_trigger(order_type: str | None) -> TriggerKind:
    if order_type in TAKE_PROFIT_TYPES:
        return TriggerKind.TAKE_PROFIT
    if order_type in STOP_LOSS_TYPES:
        return TriggerKind.STOP_LOSS
    return TriggerKind.OTHER


# ── Candle intervals actually supported by Hyperliquid ─────────
CANDLE_INTERVALS = (
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M",
)

# ── Monitoring windows ─────────────────────────────────────────
#: These are *event / position observation windows*, deliberately NOT presented
#: as candle timeframes — Hyperliquid has no 2m/4m/10m/20m candles.
MONITOR_WINDOWS: dict[str, int] = {
    "2M": 120,
    "3M": 180,
    "4M": 240,
    "5M": 300,
    "10M": 600,
    "20M": 1200,
    "30M": 1800,
    "1H": 3600,
    "4H": 14400,
}
DEFAULT_WINDOW = "30M"


def window_seconds(label: str) -> int:
    return MONITOR_WINDOWS.get(label.upper(), MONITOR_WINDOWS[DEFAULT_WINDOW])
