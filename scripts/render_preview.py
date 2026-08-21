"""Render smoke test: prints every alert layout to a UTF-8 file for eyeballing.

Not part of the production image (see .dockerignore). Uses hand-built events, so
it exercises the renderer only -- it never claims to be real market data.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("BOT_TOKEN", "0:render-smoke-test")
os.environ.setdefault("MAIN_ADMIN_ID", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./render_smoke.db")

from app.config import get_settings  # noqa: E402
from app.utils.formatting import DataPoint  # noqa: E402
from app.whale.events import EventType, ValueKind, WhaleEvent  # noqa: E402

WHEN = datetime(2026, 8, 21, 8, 31, 42, tzinfo=timezone.utc)
WALLET = "0x1234567890abcdef1234567890abcdefabcd"


def _service():
    from app.services.alert_service import AlertService

    return AlertService.__new__(AlertService)


def long_position() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.POSITION_OPENED,
        coin="BTC",
        notional=4_820_000.0,
        value_kind=ValueKind.POSITION_NOTIONAL,
        event_time=WHEN,
        side="LONG",
        wallet=WALLET,
        detection="Large Position",
    )
    event.set_many(
        {
            "position_value": DataPoint.confirmed(4_820_000.0),
            "position_size": DataPoint.confirmed(76.5),
            "entry_px": DataPoint.confirmed(63_000.0),
            "leverage": DataPoint.confirmed(10.0),
            "leverage_type": DataPoint.confirmed("cross"),
            "liquidation_px": DataPoint.confirmed(57_421.0),
            "current_px": DataPoint.confirmed(63_420.0),
            "distance_pct": DataPoint.estimated(-0.66, "computed from mark price"),
            "unrealized_pnl": DataPoint.confirmed(32_130.0),
            "margin_used": DataPoint.confirmed(482_000.0),
            "take_profit_px": DataPoint.confirmed(66_000.0),
            "stop_loss_px": DataPoint.unavailable("no resting trigger order detected"),
        }
    )
    return event


def short_no_tpsl() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.POSITION_OPENED,
        coin="ETH",
        notional=3_100_000.0,
        value_kind=ValueKind.POSITION_NOTIONAL,
        event_time=WHEN,
        side="SHORT",
        wallet=WALLET,
        detection="Large Position",
    )
    event.set_many(
        {
            "position_value": DataPoint.confirmed(3_100_000.0),
            "entry_px": DataPoint.confirmed(3_120.55),
            "leverage": DataPoint.confirmed(5.0),
            "leverage_type": DataPoint.confirmed("isolated"),
            "liquidation_px": DataPoint.unavailable("not reported by clearinghouseState"),
            "current_px": DataPoint.confirmed(3_101.0),
            "distance_pct": DataPoint.estimated(-0.63, "computed from mark price"),
            "take_profit_px": DataPoint.unavailable("trigger orders not fetched for this wallet"),
            "stop_loss_px": DataPoint.unavailable("trigger orders not fetched for this wallet"),
            "observed_for": DataPoint.estimated(1200.0, "since first seen by this monitor"),
        }
    )
    return event


def trade() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.WHALE_TRADE,
        coin="SOL",
        notional=2_450_000.0,
        value_kind=ValueKind.TRADE_VALUE,
        event_time=WHEN,
        side="BUY",
        wallet=WALLET,
        detection="Large market trade",
    )
    event.set_many(
        {
            "price": DataPoint.confirmed(182.44),
            "size": DataPoint.confirmed(13_430.0),
            "taker_side": DataPoint.confirmed("BUY"),
            "current_px": DataPoint.confirmed(182.51),
            "position_value": DataPoint.unavailable("position not fetched yet"),
            "entry_px": DataPoint.unavailable("position not fetched yet"),
            "take_profit_px": DataPoint.unavailable("trigger orders not fetched for this wallet"),
            "stop_loss_px": DataPoint.unavailable("trigger orders not fetched for this wallet"),
        }
    )
    return event


def increase() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.POSITION_INCREASED,
        coin="BTC",
        notional=1_240_000.0,
        value_kind=ValueKind.POSITION_DELTA,
        event_time=WHEN,
        side="LONG",
        wallet=WALLET,
        detection="Position increased",
    )
    event.set_many(
        {
            "delta_value": DataPoint.estimated(1_240_000.0, "size delta x mark price"),
            "position_value": DataPoint.confirmed(6_060_000.0),
            "entry_px": DataPoint.confirmed(63_180.0),
            "leverage": DataPoint.confirmed(10.0),
            "liquidation_px": DataPoint.confirmed(57_610.0),
            "current_px": DataPoint.confirmed(63_420.0),
            "take_profit_px": DataPoint.unavailable("no resting trigger order detected"),
            "stop_loss_px": DataPoint.confirmed(61_500.0),
            "observed_for": DataPoint.estimated(185.0, "since first seen by this monitor"),
        }
    )
    return event


def closed() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.POSITION_CLOSED,
        coin="BTC",
        notional=4_820_000.0,
        value_kind=ValueKind.POSITION_NOTIONAL,
        event_time=WHEN,
        side="LONG",
        wallet=WALLET,
        detection="Position closed",
    )
    event.set_many(
        {
            "closed_position_value": DataPoint.confirmed(4_820_000.0),
            "entry_px": DataPoint.confirmed(63_000.0),
            "final_unrealized_pnl": DataPoint.confirmed(-18_400.0),
            "current_px": DataPoint.confirmed(62_760.0),
            "observed_for": DataPoint.estimated(3_600.0, "since first seen by this monitor"),
        }
    )
    return event


def limit_order() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.ORDER_PLACED,
        coin="BTC",
        notional=3_250_000.0,
        value_kind=ValueKind.ORDER_NOTIONAL,
        event_time=WHEN,
        side="BUY",
        wallet=WALLET,
        detection="Large resting order",
        order_id=987654321,
        status="open",
    )
    event.set_many(
        {
            "price": DataPoint.confirmed(63_000.0),
            "size": DataPoint.confirmed(51.58),
            "current_px": DataPoint.confirmed(63_420.0),
            "distance_pct": DataPoint.estimated(-0.66, "computed from mark price"),
            "order_status": DataPoint.confirmed("open"),
            "order_type": DataPoint.confirmed("Limit"),
            "tif": DataPoint.confirmed("Gtc"),
        }
    )
    return event


def partial_fill() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.ORDER_PARTIALLY_FILLED,
        coin="BTC",
        notional=3_250_000.0,
        value_kind=ValueKind.ORDER_NOTIONAL,
        event_time=WHEN,
        side="BUY",
        wallet=WALLET,
        detection="Order partially filled",
        order_id=987654321,
        status="partially_filled",
    )
    event.set_many(
        {
            "price": DataPoint.confirmed(63_000.0),
            "orig_notional": DataPoint.confirmed(3_250_000.0),
            "filled_notional": DataPoint.estimated(1_150_000.0, "filled size x limit price"),
            "remaining_notional": DataPoint.estimated(2_100_000.0, "remaining size x limit price"),
            "current_px": DataPoint.confirmed(63_010.0),
            "order_status": DataPoint.confirmed("partially filled"),
            "resting_for": DataPoint.confirmed(192.0),
        }
    )
    return event


def cancelled() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.ORDER_CANCELLED,
        coin="BTC",
        notional=3_250_000.0,
        value_kind=ValueKind.ORDER_NOTIONAL,
        event_time=WHEN,
        side="BUY",
        wallet=WALLET,
        detection="Order cancelled",
        order_id=987654321,
        status="canceled",
    )
    event.set_many(
        {
            "price": DataPoint.confirmed(63_000.0),
            "orig_notional": DataPoint.confirmed(3_250_000.0),
            "remaining_notional": DataPoint.estimated(2_100_000.0, "remaining size x limit price"),
            "current_px": DataPoint.confirmed(63_420.0),
            "order_status": DataPoint.confirmed("canceled"),
            "status_note": DataPoint.unavailable(
                "cancel vs fill unresolved: orderStatus lookup unavailable"
            ),
        }
    )
    return event


def book_level() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.BOOK_LEVEL,
        coin="BTC",
        notional=8_400_000.0,
        value_kind=ValueKind.BOOK_LEVEL_NOTIONAL,
        event_time=WHEN,
        side="BUY",
        detection="Large book level",
    )
    event.set_many(
        {
            "price": DataPoint.confirmed(62_500.0),
            "current_px": DataPoint.confirmed(63_420.0),
            "distance_pct": DataPoint.estimated(-1.45, "computed from mark price"),
            "orders_at_level": DataPoint.confirmed(4),
            "wallet_attribution": DataPoint.unavailable("l2Book levels are aggregated"),
        }
    )
    return event


CASES = [
    ("LARGE LONG (spec 4A / 16)", long_position),
    ("LARGE SHORT, no TP/SL available (spec 4B)", short_no_tpsl),
    ("LARGE MARKET TRADE, no position context yet", trade),
    ("POSITION INCREASED", increase),
    ("POSITION CLOSED", closed),
    ("LARGE LIMIT ORDER (spec 5 / 17)", limit_order),
    ("ORDER PARTIALLY FILLED (spec 18)", partial_fill),
    ("ORDER CANCELLED (spec 18)", cancelled),
    ("LARGE BOOK LEVEL, no wallet attribution", book_level),
]


def main() -> None:
    get_settings()
    service = _service()
    chunks = []
    for title, builder in CASES:
        chunks.append(f"### {title}\n")
        chunks.append(service.render(builder()))
        chunks.append("\n")
    out = "\n".join(chunks)
    with open("render_preview.txt", "w", encoding="utf-8") as handle:
        handle.write(out)
    print(f"wrote render_preview.txt ({len(out)} chars, {len(CASES)} layouts)")


if __name__ == "__main__":
    main()
