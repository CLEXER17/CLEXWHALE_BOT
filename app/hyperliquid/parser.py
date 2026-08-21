"""Defensive parsers: raw Hyperliquid JSON → typed models.

Every parser returns ``None`` (or skips the item) rather than raising on
malformed input — a single bad element must never take down an ingest loop.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.hyperliquid.models import (
    AccountState,
    AssetContext,
    AssetMeta,
    BookLevel,
    Fill,
    L2Book,
    OpenOrder,
    OrderUpdate,
    Position,
    Trade,
)
from app.utils.formatting import from_ms, is_hex_address, to_float
from app.utils.logging import get_logger

log = get_logger(__name__)


def is_spot_coin(coin: str) -> bool:
    """Spot assets are addressed as ``@107``-style indices."""
    return bool(coin) and coin.startswith("@")


def is_builder_perp(coin: str) -> bool:
    """HIP-3 builder-deployed perps are prefixed, e.g. ``xyz:XYZ100``."""
    return ":" in (coin or "")


def normalise_coin(coin: Any) -> str | None:
    if not isinstance(coin, str) or not coin.strip():
        return None
    return coin.strip()


# ── market data ────────────────────────────────────────────────

def parse_trade(raw: Any) -> Trade | None:
    if not isinstance(raw, dict):
        return None
    coin = normalise_coin(raw.get("coin"))
    px = to_float(raw.get("px"))
    sz = to_float(raw.get("sz"))
    if not coin or px is None or sz is None:
        return None
    users = raw.get("users") or []
    buyer = users[0] if len(users) > 0 and is_hex_address(users[0]) else None
    seller = users[1] if len(users) > 1 and is_hex_address(users[1]) else None
    tid = raw.get("tid")
    return Trade(
        coin=coin,
        side=raw.get("side"),
        px=px,
        sz=sz,
        time=from_ms(raw.get("time")),
        tid=int(tid) if isinstance(tid, (int, float)) else None,
        hash=raw.get("hash"),
        buyer=buyer,
        seller=seller,
    )


def parse_trades(raw: Any) -> list[Trade]:
    if not isinstance(raw, list):
        return []
    return [t for t in (parse_trade(item) for item in raw) if t is not None]


def parse_level(raw: Any) -> BookLevel | None:
    if not isinstance(raw, dict):
        return None
    px = to_float(raw.get("px"))
    sz = to_float(raw.get("sz"))
    if px is None or sz is None:
        return None
    n = raw.get("n")
    return BookLevel(px=px, sz=sz, n=int(n) if isinstance(n, (int, float)) else 0)


def parse_l2_book(raw: Any) -> L2Book | None:
    if not isinstance(raw, dict):
        return None
    coin = normalise_coin(raw.get("coin"))
    levels = raw.get("levels")
    if not coin or not isinstance(levels, list) or len(levels) < 2:
        return None

    def side(items: Any) -> list[BookLevel]:
        if not isinstance(items, list):
            return []
        return [lvl for lvl in (parse_level(i) for i in items) if lvl is not None]

    return L2Book(coin=coin, time=from_ms(raw.get("time")), bids=side(levels[0]), asks=side(levels[1]))


def parse_meta(raw: Any) -> list[AssetMeta]:
    universe = raw.get("universe") if isinstance(raw, dict) else None
    if not isinstance(universe, list):
        return []
    out: list[AssetMeta] = []
    for item in universe:
        if not isinstance(item, dict):
            continue
        name = normalise_coin(item.get("name"))
        if not name:
            continue
        out.append(
            AssetMeta(
                name=name,
                sz_decimals=int(to_float(item.get("szDecimals"), 0) or 0),
                max_leverage=to_float(item.get("maxLeverage")),
                only_isolated=bool(item.get("onlyIsolated", False)),
            )
        )
    return out


def parse_asset_context(coin: str, raw: Any) -> AssetContext | None:
    if not isinstance(raw, dict):
        return None
    return AssetContext(
        coin=coin,
        mark_px=to_float(raw.get("markPx")),
        mid_px=to_float(raw.get("midPx")),
        oracle_px=to_float(raw.get("oraclePx")),
        prev_day_px=to_float(raw.get("prevDayPx")),
        funding=to_float(raw.get("funding")),
        open_interest=to_float(raw.get("openInterest")),
        day_ntl_vlm=to_float(raw.get("dayNtlVlm")),
    )


def parse_meta_and_asset_ctxs(raw: Any) -> tuple[list[AssetMeta], dict[str, AssetContext]]:
    """``metaAndAssetCtxs`` → ``[meta, ctxs]`` positionally aligned with universe."""
    if not isinstance(raw, list) or len(raw) < 2:
        return [], {}
    metas = parse_meta(raw[0])
    ctxs_raw = raw[1] if isinstance(raw[1], list) else []
    contexts: dict[str, AssetContext] = {}
    for meta, ctx_raw in zip(metas, ctxs_raw):
        ctx = parse_asset_context(meta.name, ctx_raw)
        if ctx is not None:
            contexts[meta.name] = ctx
    return metas, contexts


def parse_all_mids(raw: Any) -> dict[str, float]:
    mids = raw.get("mids") if isinstance(raw, dict) and "mids" in raw else raw
    if not isinstance(mids, dict):
        return {}
    out: dict[str, float] = {}
    for coin, value in mids.items():
        price = to_float(value)
        if price is not None and isinstance(coin, str):
            out[coin] = price
    return out


# ── account data ───────────────────────────────────────────────

def parse_position(raw: Any) -> Position | None:
    if not isinstance(raw, dict):
        return None
    coin = normalise_coin(raw.get("coin"))
    szi = to_float(raw.get("szi"))
    if not coin or szi is None:
        return None
    leverage = raw.get("leverage") if isinstance(raw.get("leverage"), dict) else {}
    funding = raw.get("cumFunding") if isinstance(raw.get("cumFunding"), dict) else {}
    return Position(
        coin=coin,
        szi=szi,
        entry_px=to_float(raw.get("entryPx")),
        position_value=to_float(raw.get("positionValue")),
        unrealized_pnl=to_float(raw.get("unrealizedPnl")),
        return_on_equity=to_float(raw.get("returnOnEquity")),
        liquidation_px=to_float(raw.get("liquidationPx")),
        margin_used=to_float(raw.get("marginUsed")),
        max_leverage=to_float(raw.get("maxLeverage")),
        leverage_value=to_float(leverage.get("value")),
        leverage_type=leverage.get("type") if isinstance(leverage.get("type"), str) else None,
        funding_since_open=to_float(funding.get("sinceOpen")),
    )


def parse_clearinghouse_state(user: str, raw: Any) -> AccountState | None:
    if not isinstance(raw, dict):
        return None
    # The websocket wraps the payload one level deeper than REST does.
    inner = raw.get("clearinghouseState") if isinstance(raw.get("clearinghouseState"), dict) else raw
    margin = inner.get("marginSummary") if isinstance(inner.get("marginSummary"), dict) else {}
    positions: dict[str, Position] = {}
    for entry in inner.get("assetPositions") or []:
        if not isinstance(entry, dict):
            continue
        position = parse_position(entry.get("position"))
        if position is not None and position.szi != 0:
            positions[position.coin] = position
    return AccountState(
        user=user,
        positions=positions,
        account_value=to_float(margin.get("accountValue")),
        total_ntl_pos=to_float(margin.get("totalNtlPos")),
        total_margin_used=to_float(margin.get("totalMarginUsed")),
        withdrawable=to_float(inner.get("withdrawable")),
        time=from_ms(inner.get("time")),
    )


def parse_open_order(raw: Any) -> OpenOrder | None:
    if not isinstance(raw, dict):
        return None
    coin = normalise_coin(raw.get("coin"))
    oid = raw.get("oid")
    sz = to_float(raw.get("sz"))
    if not coin or not isinstance(oid, (int, float)) or sz is None:
        return None
    trigger_condition = raw.get("triggerCondition")
    if isinstance(trigger_condition, str) and trigger_condition.strip().upper() in {"N/A", ""}:
        trigger_condition = None
    is_trigger = bool(raw.get("isTrigger", False))
    # Plain limit orders report ``triggerPx: "0.0"``; that is not a real level.
    trigger_px = to_float(raw.get("triggerPx")) if is_trigger else None
    children = [
        child
        for child in (parse_open_order(c) for c in (raw.get("children") or []))
        if child is not None
    ]
    return OpenOrder(
        coin=coin,
        oid=int(oid),
        side=raw.get("side"),
        limit_px=to_float(raw.get("limitPx")),
        sz=sz,
        orig_sz=to_float(raw.get("origSz")),
        timestamp=from_ms(raw.get("timestamp")),
        order_type=raw.get("orderType") if isinstance(raw.get("orderType"), str) else None,
        reduce_only=bool(raw.get("reduceOnly", False)),
        is_trigger=bool(raw.get("isTrigger", False)),
        trigger_px=to_float(raw.get("triggerPx")),
        trigger_condition=trigger_condition,
        is_position_tpsl=bool(raw.get("isPositionTpsl", False)),
        tif=raw.get("tif") if isinstance(raw.get("tif"), str) else None,
        cloid=raw.get("cloid") if isinstance(raw.get("cloid"), str) else None,
    )


def parse_open_orders(raw: Any) -> list[OpenOrder]:
    if not isinstance(raw, list):
        return []
    return [o for o in (parse_open_order(item) for item in raw) if o is not None]


def parse_order_update(raw: Any) -> OrderUpdate | None:
    if not isinstance(raw, dict):
        return None
    order = raw.get("order")
    status = raw.get("status")
    if not isinstance(order, dict) or not isinstance(status, str):
        return None
    coin = normalise_coin(order.get("coin"))
    oid = order.get("oid")
    sz = to_float(order.get("sz"))
    if not coin or not isinstance(oid, (int, float)) or sz is None:
        return None
    return OrderUpdate(
        coin=coin,
        oid=int(oid),
        side=order.get("side"),
        limit_px=to_float(order.get("limitPx")),
        sz=sz,
        orig_sz=to_float(order.get("origSz")),
        timestamp=from_ms(order.get("timestamp")),
        status=status,
        status_timestamp=from_ms(raw.get("statusTimestamp")),
        cloid=order.get("cloid") if isinstance(order.get("cloid"), str) else None,
    )


def parse_order_updates(raw: Any) -> list[OrderUpdate]:
    if not isinstance(raw, list):
        return []
    return [o for o in (parse_order_update(item) for item in raw) if o is not None]


def parse_fill(raw: Any) -> Fill | None:
    if not isinstance(raw, dict):
        return None
    coin = normalise_coin(raw.get("coin"))
    px = to_float(raw.get("px"))
    sz = to_float(raw.get("sz"))
    if not coin or px is None or sz is None:
        return None
    oid, tid = raw.get("oid"), raw.get("tid")
    return Fill(
        coin=coin,
        px=px,
        sz=sz,
        side=raw.get("side"),
        time=from_ms(raw.get("time")),
        oid=int(oid) if isinstance(oid, (int, float)) else None,
        tid=int(tid) if isinstance(tid, (int, float)) else None,
        hash=raw.get("hash"),
        dir=raw.get("dir") if isinstance(raw.get("dir"), str) else None,
        closed_pnl=to_float(raw.get("closedPnl")),
        start_position=to_float(raw.get("startPosition")),
        crossed=bool(raw.get("crossed", False)),
        fee=to_float(raw.get("fee")),
        liquidation=raw.get("liquidation") if isinstance(raw.get("liquidation"), dict) else None,
    )


def parse_fills(raw: Any) -> list[Fill]:
    items: Iterable[Any]
    if isinstance(raw, dict):
        items = raw.get("fills") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    return [f for f in (parse_fill(item) for item in items) if f is not None]
