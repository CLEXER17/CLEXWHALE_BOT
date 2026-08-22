"""Forced liquidations: attribution, side provenance, and what is not claimed.

Feature item 5. Three things are easy to get wrong about a liquidation and each
one has a test here:

* a ``liquidation`` object on wallet X's fills feed does not mean X was
  liquidated — Hyperliquid names the party in ``liquidation.liquidatedUser``;
* ``closedPnl`` on that fill is the *subscribed* wallet's result, so it is only
  the liquidated party's loss when the two are the same wallet;
* the side that was closed may never be read off the fill's BUY/SELL direction.

Leverage and margin at the moment of liquidation are not on any public feed
(`.agent/API_NOTES.md` §5), so they must be absent rather than approximated.
"""

from __future__ import annotations

from app.services.alert_service import AlertService
from app.utils.formatting import Confidence
from app.whale.detector import WhaleDetector, _stated_direction
from app.whale.events import EventType, ValueKind
from app.whale.lifecycle import NEVER_MODIFY_POSITION, may_modify_position
from tests.factories import (
    WALLET_A,
    WALLET_B,
    make_context,
    make_liquidation_fill,
    make_position,
    price_map,
)

PRICES = price_map()


def detector() -> WhaleDetector:
    return WhaleDetector(lambda coin: PRICES.get(coin))


# ── the event itself ───────────────────────────────────────────

def test_a_liquidation_fill_becomes_a_liquidation_event():
    fill = make_liquidation_fill(px=95_000.0, sz=60.0)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.event_type is EventType.WHALE_LIQUIDATED
    assert event.coin == "BTC"
    assert event.wallet == WALLET_A.lower()


def test_the_value_is_the_liquidation_fill_notional():
    fill = make_liquidation_fill(px=95_000.0, sz=60.0)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.notional == 95_000.0 * 60.0
    assert event.value_kind is ValueKind.LIQUIDATION_VALUE


def test_a_liquidation_is_measured_against_the_position_threshold():
    """No fifth threshold class: a forced close is position news."""
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill())
    assert event is not None
    assert event.threshold_class == "position"


def test_a_small_liquidation_below_the_threshold_is_not_an_event():
    fill = make_liquidation_fill(px=95_000.0, sz=1.0)  # $95k
    assert detector().from_liquidation(WALLET_A, fill, min_notional=5_000_000) is None


def test_price_and_size_come_from_the_fill():
    fill = make_liquidation_fill(px=94_321.0, sz=12.5)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.numeric("price") == 94_321.0
    assert event.numeric("size") == 12.5
    assert event.confidence("price") is Confidence.CONFIRMED


def test_mark_price_and_method_are_reported_when_hyperliquid_sends_them():
    fill = make_liquidation_fill(mark_px=95_010.0, method="market")
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.numeric("liquidation_mark_px") == 95_010.0
    assert event.value("liquidation_method") == "market"


def test_a_liquidation_object_without_a_mark_price_reports_it_unavailable():
    fill = make_liquidation_fill(mark_px=None, method=None)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert not event.has("liquidation_mark_px")
    assert not event.has("liquidation_method")
    assert event.confidence("liquidation_mark_px") is Confidence.UNAVAILABLE


# ── who was liquidated ─────────────────────────────────────────

def test_liquidated_user_names_the_subject_not_the_subscribed_wallet():
    """B was force-closed; A merely happened to be on the other side."""
    fill = make_liquidation_fill(liquidated_user=WALLET_B)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.wallet == WALLET_B.lower()
    assert event.context["counterparty"] == WALLET_A


def test_without_liquidated_user_the_subscribed_wallet_is_the_subject():
    fill = make_liquidation_fill(liquidated_user=None)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.wallet == WALLET_A.lower()
    assert event.context["counterparty"] is None


def test_closed_pnl_is_only_reported_when_the_subject_is_the_subscribed_wallet():
    """``closedPnl`` belongs to the feed's owner, and A is not the liquidated party."""
    fill = make_liquidation_fill(liquidated_user=WALLET_B, closed_pnl=-180_000.0)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert not event.has("realized_pnl")
    assert "counterparty" in (event.point("realized_pnl").note or "")


def test_closed_pnl_is_reported_for_the_wallet_that_was_liquidated():
    fill = make_liquidation_fill(liquidated_user=WALLET_A, closed_pnl=-180_000.0)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.numeric("realized_pnl") == -180_000.0
    assert event.confidence("realized_pnl") is Confidence.CONFIRMED


def test_a_missing_closed_pnl_is_not_invented():
    fill = make_liquidation_fill(closed_pnl=None)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert not event.has("realized_pnl")


def test_a_counterpartys_position_snapshot_is_never_attached():
    """We hold a snapshot for A, but the event is about B."""
    context = make_context(position=make_position(szi=60.0))
    fill = make_liquidation_fill(liquidated_user=WALLET_B, start_position=None, dir=None)
    event = detector().from_liquidation(WALLET_A, fill, context=context)
    assert event is not None
    assert not event.has("position_value")
    assert not event.has("liquidated_side")


# ── which side was closed ──────────────────────────────────────

def test_the_side_comes_from_the_last_verified_position_snapshot():
    context = make_context(position=make_position(szi=-60.0))  # SHORT
    fill = make_liquidation_fill(side="B", start_position=None, dir=None)
    event = detector().from_liquidation(WALLET_A, fill, context=context)
    assert event is not None
    assert event.side == "SHORT"
    assert "snapshot" in (event.point("liquidated_side").note or "")


def test_the_side_falls_back_to_the_sign_of_start_position():
    fill = make_liquidation_fill(side="A", start_position=-42.0, dir=None)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.side == "SHORT"
    assert "startPosition" in (event.point("liquidated_side").note or "")


def test_the_side_falls_back_to_the_direction_hyperliquid_states():
    fill = make_liquidation_fill(side="B", start_position=None, dir="Close Short")
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.side == "SHORT"


def test_with_no_verified_source_the_side_is_unavailable_not_inferred():
    """A SELL fill is not evidence of a long. Nothing is claimed."""
    fill = make_liquidation_fill(side="A", start_position=None, dir="Sell")
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.side is None
    assert not event.has("liquidated_side")
    assert event.confidence("liquidated_side") is Confidence.UNAVAILABLE


def test_a_flat_start_position_is_not_a_side():
    fill = make_liquidation_fill(side="A", start_position=0.0, dir=None)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.side is None


def test_the_fill_direction_is_kept_as_an_order_side_word():
    """BUY/SELL and LONG/SHORT live in different fields, always."""
    fill = make_liquidation_fill(side="A", start_position=-42.0)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert event.value("trade_side") == "SELL"
    assert event.side == "SHORT"


def test_stated_direction_reads_only_closing_descriptions():
    assert _stated_direction("Close Long") == "LONG"
    assert _stated_direction("Close Short") == "SHORT"
    # A flip closes the side named first.
    assert _stated_direction("Long > Short") == "LONG"
    # An opening names a side, but not one that was closed.
    assert _stated_direction("Open Long") is None
    # Execution directions are never translated into position sides.
    assert _stated_direction("Buy") is None
    assert _stated_direction("Sell") is None
    assert _stated_direction(None) is None
    assert _stated_direction("") is None


# ── what a liquidation may not do ──────────────────────────────

def test_a_liquidation_may_never_write_position_state():
    """The only snapshot it could write is the *pre*-liquidation one."""
    context = make_context(position=make_position(szi=60.0))
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill(), context=context)
    assert event is not None
    assert EventType.WHALE_LIQUIDATED in NEVER_MODIFY_POSITION
    assert may_modify_position(event) is False


def test_a_liquidation_is_not_a_position_lifecycle_event():
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill())
    assert event is not None
    assert event.is_position_event is False
    assert event.is_order_event is False


def test_leverage_and_margin_at_liquidation_are_never_reported():
    """Hyperliquid does not publish them, and the old snapshot is not them."""
    context = make_context(position=make_position(leverage_value=20.0, margin_used=250_000.0))
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill(), context=context)
    assert event is not None
    assert not event.has("leverage")
    assert not event.has("margin_used")


def test_entry_and_liquidation_price_of_the_dead_position_are_not_reported():
    context = make_context(position=make_position(entry_px=98_000.0, liquidation_px=94_500.0))
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill(), context=context)
    assert event is not None
    assert not event.has("entry_px")
    assert not event.has("liquidation_px")


# ── the rendered alert ─────────────────────────────────────────

def render(event) -> str:
    """Render without a live stack: ``render`` reads only the event."""
    return AlertService.render(AlertService.__new__(AlertService), event)


def test_the_alert_leads_with_the_liquidation_header():
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill(px=95_000.0, sz=60.0))
    assert event is not None
    text = render(event)
    assert text.startswith("💥 WHALE LIQUIDATED")
    assert text.endswith("🐋 Whale Monitor")
    assert "💥 <b>Liquidated value:</b> $5,700,000" in text
    assert "🔎 <b>Detection:</b> Forced liquidation" in text


def test_an_unknown_side_is_printed_as_unavailable_with_its_reason():
    fill = make_liquidation_fill(side="A", start_position=None, dir="Sell")
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    text = render(event)
    assert "<b>Liquidated side:</b> N/A" in text
    # The execution side is still shown, as an execution side.
    assert "🔀 <b>Fill direction:</b> SELL" in text
    assert "📈 LONG" not in text and "📉 SHORT" not in text


def test_a_known_side_is_badged_as_a_position_side():
    fill = make_liquidation_fill(side="B", start_position=-42.0)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    assert "📉 SHORT liquidated" in render(event)


def test_the_alert_says_why_pnl_is_missing_when_it_belongs_to_the_counterparty():
    fill = make_liquidation_fill(liquidated_user=WALLET_B, closed_pnl=-180_000.0)
    event = detector().from_liquidation(WALLET_A, fill)
    assert event is not None
    text = render(event)
    assert "⚪ <b>Realized PnL:</b> N/A — closedPnl belongs to the counterparty" in text
    assert "-$180,000" not in text


def test_the_alert_never_prints_leverage_margin_or_tpsl_lines():
    context = make_context(position=make_position(leverage_value=20.0))
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill(), context=context)
    assert event is not None
    text = render(event)
    for absent in ("⚡ <b>Leverage:", "🏦 <b>Margin:", "🎯 <b>TP:", "🛑 <b>SL:"):
        assert absent not in text
    assert "Leverage and margin at liquidation are not reported by Hyperliquid" in text


def test_the_full_wallet_address_is_shown_in_monospace():
    event = detector().from_liquidation(WALLET_A, make_liquidation_fill())
    assert event is not None
    assert f"👤 <b>Trader:</b> <code>{WALLET_A.lower()}</code>" in render(event)
