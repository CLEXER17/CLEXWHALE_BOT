"""Threshold, coin, margin and detector-toggle gating.

Spec §36: "Coin filtering", "Whale threshold detection". Spec §7: each
``ValueKind`` gets its own minimum so a position notional is never compared
against a cash-flow threshold.
"""

from __future__ import annotations

from app.services.settings_service import RuntimeConfig
from app.whale.events import DataPoint, EventType, ValueKind, WhaleEvent
from app.whale.filters import (
    REASON_CANCELS_OFF,
    REASON_COIN,
    REASON_DETECTOR,
    REASON_MARGIN,
    REASON_MARGIN_UNKNOWN,
    REASON_MONITORING_OFF,
    REASON_OK,
    REASON_THRESHOLD,
    WhaleFilter,
)


class StubSettings:
    """Stands in for ``SettingsService`` — the filter only reads ``.config``."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config


def make_filter(config: RuntimeConfig | None = None) -> WhaleFilter:
    return WhaleFilter(StubSettings(config or RuntimeConfig()))


def event(
    *,
    event_type: EventType = EventType.WHALE_TRADE,
    value_kind: ValueKind = ValueKind.TRADE_VALUE,
    coin: str = "BTC",
    notional: float = 3_000_000.0,
    margin: float | None = None,
) -> WhaleEvent:
    whale = WhaleEvent(
        event_type=event_type,
        coin=coin,
        notional=notional,
        value_kind=value_kind,
        side="LONG",
        wallet="0x" + "11" * 20,
        detection="test",
    )
    if margin is not None:
        whale.set("margin_used", DataPoint.confirmed(margin))
    return whale


def config(**overrides) -> RuntimeConfig:
    overrides.setdefault("all_coins", True)
    return RuntimeConfig(**overrides)


# ── thresholds ─────────────────────────────────────────────────

def test_above_threshold_is_accepted():
    result = make_filter().evaluate(event(notional=3_000_000), config(min_whale_value=2_000_000))
    assert result.accepted is True
    assert result.reason == REASON_OK
    assert result.threshold == 2_000_000


def test_below_threshold_is_rejected_with_a_reason():
    result = make_filter().evaluate(event(notional=1_999_999), config(min_whale_value=2_000_000))
    assert result.accepted is False
    assert result.reason == REASON_THRESHOLD
    assert result.threshold == 2_000_000


def test_exactly_at_threshold_is_accepted():
    result = make_filter().evaluate(event(notional=2_000_000), config(min_whale_value=2_000_000))
    assert result.accepted is True


def test_per_class_threshold_overrides_the_global_one():
    """A $3M order can be uninteresting while a $3M trade is not."""
    cfg = config(min_whale_value=2_000_000, min_order_value=10_000_000)
    order = event(
        event_type=EventType.ORDER_PLACED,
        value_kind=ValueKind.ORDER_NOTIONAL,
        notional=3_000_000,
    )
    assert make_filter().evaluate(order, cfg).accepted is False
    # The trade class is untouched by the order override.
    assert make_filter().evaluate(event(notional=3_000_000), cfg).accepted is True


def test_position_notional_and_position_delta_have_independent_minimums():
    cfg = config(
        min_whale_value=2_000_000,
        min_position_value=20_000_000,
        min_position_delta_value=1_000_000,
    )
    whole = event(
        event_type=EventType.POSITION_CLOSED,
        value_kind=ValueKind.POSITION_NOTIONAL,
        notional=5_000_000,
    )
    delta = event(
        event_type=EventType.POSITION_INCREASED,
        value_kind=ValueKind.POSITION_DELTA,
        notional=1_500_000,
    )
    assert make_filter().evaluate(whole, cfg).accepted is False
    assert make_filter().evaluate(delta, cfg).accepted is True


def test_effective_thresholds_fall_back_to_the_global_value():
    cfg = config(min_whale_value=2_000_000, min_order_value=5_000_000)
    thresholds = cfg.effective_thresholds
    assert thresholds["order"] == 5_000_000
    assert thresholds["trade"] == 2_000_000
    assert thresholds["position"] == 2_000_000
    assert cfg.lowest_threshold == 2_000_000


def test_lowest_threshold_is_the_cheapest_gate():
    cfg = config(min_whale_value=9_000_000, min_position_delta_value=500_000)
    assert cfg.lowest_threshold == 500_000


def test_book_level_falls_back_to_the_global_threshold():
    cfg = config(min_whale_value=2_000_000, enable_book_scanner=True)
    book = event(
        event_type=EventType.BOOK_LEVEL,
        value_kind=ValueKind.BOOK_LEVEL_NOTIONAL,
        notional=2_500_000,
    )
    assert make_filter().evaluate(book, cfg).accepted is True


# ── coin filter ────────────────────────────────────────────────

def test_untracked_coin_is_rejected():
    cfg = config(coins=("BTC", "ETH"), all_coins=False)
    result = make_filter().evaluate(event(coin="DOGE"), cfg)
    assert result.accepted is False
    assert result.reason == REASON_COIN


def test_tracked_coin_is_accepted():
    cfg = config(coins=("BTC", "ETH"), all_coins=False)
    assert make_filter().evaluate(event(coin="ETH"), cfg).accepted is True


def test_coin_matching_is_case_insensitive():
    cfg = config(coins=("BTC",), all_coins=False)
    assert cfg.coin_enabled("btc") is True


def test_all_coins_mode_accepts_anything():
    cfg = config(coins=(), all_coins=True)
    assert make_filter().evaluate(event(coin="WIF"), cfg).accepted is True
    assert cfg.coin_label == "ALL COINS"


def test_empty_coin_list_without_all_coins_tracks_nothing():
    """An admin who clears the list monitors nothing — it must not mean "all"."""
    cfg = config(coins=(), all_coins=False)
    assert cfg.coin_enabled("BTC") is False
    assert cfg.coin_label == "none selected"


# ── monitoring switch and detector toggles ─────────────────────

def test_monitoring_off_rejects_everything_first():
    cfg = config(monitoring_enabled=False, coins=(), all_coins=False)
    result = make_filter().evaluate(event(coin="DOGE", notional=1.0), cfg)
    assert result.accepted is False
    # Monitoring is checked before coin and threshold, so the reason is exact.
    assert result.reason == REASON_MONITORING_OFF


def test_disabled_detector_rejects_its_own_events_only():
    cfg = config(enable_order_detector=False)
    order = event(event_type=EventType.ORDER_PLACED, value_kind=ValueKind.ORDER_NOTIONAL)
    result = make_filter().evaluate(order, cfg)
    assert result.accepted is False
    assert result.reason == REASON_DETECTOR
    assert make_filter().evaluate(event(), cfg).accepted is True


def test_cancel_alerts_can_be_turned_off_without_disabling_orders():
    cfg = config(enable_order_detector=True, enable_order_cancel_alerts=False)
    cancel = event(event_type=EventType.ORDER_CANCELLED, value_kind=ValueKind.ORDER_NOTIONAL)
    placed = event(event_type=EventType.ORDER_PLACED, value_kind=ValueKind.ORDER_NOTIONAL)
    assert make_filter().evaluate(cancel, cfg).reason == REASON_CANCELS_OFF
    assert make_filter().evaluate(placed, cfg).accepted is True


def test_book_scanner_is_off_by_default():
    book = event(event_type=EventType.BOOK_LEVEL, value_kind=ValueKind.BOOK_LEVEL_NOTIONAL)
    result = make_filter().evaluate(book, config())
    assert result.accepted is False
    assert result.reason == REASON_DETECTOR


# ── margin gate ────────────────────────────────────────────────

def test_margin_gate_is_off_by_default():
    """Adding the feature must not silence an existing deployment."""
    cfg = config(min_whale_value=2_000_000)
    assert cfg.margin_gate_enabled is False
    assert make_filter().evaluate(event(notional=3_000_000), cfg).accepted is True


def test_a_position_above_the_margin_gate_is_accepted():
    cfg = config(min_whale_value=2_000_000, min_margin_value=2_000_000)
    result = make_filter().evaluate(event(notional=20_000_000, margin=2_500_000), cfg)
    assert result.accepted is True
    assert result.margin_gate == 2_000_000


def test_a_position_below_the_margin_gate_is_rejected():
    """The whole point: a $20M position at 50x carries only $400k of margin."""
    cfg = config(min_whale_value=2_000_000, min_margin_value=2_000_000)
    result = make_filter().evaluate(event(notional=20_000_000, margin=400_000), cfg)
    assert result.accepted is False
    assert result.reason == REASON_MARGIN
    assert result.margin_gate == 2_000_000


def test_exactly_at_the_margin_gate_is_accepted():
    cfg = config(min_margin_value=2_000_000)
    assert make_filter().evaluate(event(margin=2_000_000), cfg).accepted is True


def test_margin_is_never_read_from_the_notional():
    """A large notional with no margin figure must not pass as if it had one."""
    cfg = config(min_whale_value=2_000_000, min_margin_value=1_000)
    result = make_filter().evaluate(event(notional=500_000_000, margin=None), cfg)
    assert result.accepted is False
    assert result.reason == REASON_MARGIN_UNKNOWN


def test_unknown_margin_is_rejected_with_its_own_reason():
    cfg = config(min_margin_value=2_000_000)
    result = make_filter().evaluate(event(margin=None), cfg)
    assert result.accepted is False
    assert result.reason == REASON_MARGIN_UNKNOWN
    # Distinguishable from a below-gate rejection, so /status can explain it.
    assert result.reason != REASON_MARGIN


def test_resting_orders_and_book_levels_are_exempt_from_the_margin_gate():
    """No collateral is committed until a fill, so gating these is meaningless."""
    cfg = config(min_margin_value=5_000_000, enable_book_scanner=True)
    order = event(
        event_type=EventType.ORDER_PLACED,
        value_kind=ValueKind.ORDER_NOTIONAL,
        notional=3_000_000,
    )
    book = event(
        event_type=EventType.BOOK_LEVEL,
        value_kind=ValueKind.BOOK_LEVEL_NOTIONAL,
        notional=3_000_000,
    )
    assert make_filter().evaluate(order, cfg).accepted is True
    assert make_filter().evaluate(book, cfg).accepted is True


def test_a_liquidation_is_exempt_from_the_margin_gate():
    """The margin is the thing that just ran out.

    Hyperliquid reports no margin figure for the moment of a liquidation on any
    feed, so an active margin gate would read "margin unknown" and reject every
    forced close — turning the gate into an off switch for the one event it least
    applies to.
    """
    cfg = config(min_margin_value=5_000_000)
    liquidation = event(
        event_type=EventType.WHALE_LIQUIDATED,
        value_kind=ValueKind.LIQUIDATION_VALUE,
        notional=5_700_000,
        margin=None,
    )
    result = make_filter().evaluate(liquidation, cfg)
    assert result.accepted is True
    assert result.reason == REASON_OK


def test_a_liquidation_is_measured_against_the_position_threshold():
    cfg = config(min_whale_value=2_000_000, min_position_value=5_000_000)
    liquidation = event(
        event_type=EventType.WHALE_LIQUIDATED,
        value_kind=ValueKind.LIQUIDATION_VALUE,
        notional=3_000_000,
    )
    result = make_filter().evaluate(liquidation, cfg)
    assert result.accepted is False
    assert result.reason == REASON_THRESHOLD
    assert result.threshold == 5_000_000


def test_turning_position_alerts_off_also_silences_liquidations():
    """A forced close is position news; no separate toggle exists for it."""
    cfg = config(enable_position_detector=False)
    liquidation = event(
        event_type=EventType.WHALE_LIQUIDATED,
        value_kind=ValueKind.LIQUIDATION_VALUE,
        notional=5_700_000,
    )
    assert make_filter().evaluate(liquidation, cfg).reason == REASON_DETECTOR


def test_the_threshold_is_applied_before_the_margin_gate():
    """A tiny trade is rejected for being tiny, not for its margin."""
    cfg = config(min_whale_value=2_000_000, min_margin_value=2_000_000)
    result = make_filter().evaluate(event(notional=1_000, margin=None), cfg)
    assert result.reason == REASON_THRESHOLD


def test_margin_rejections_are_counted_separately():
    whale_filter = make_filter()
    cfg = config(min_whale_value=2_000_000, min_margin_value=2_000_000)
    whale_filter.evaluate(event(margin=3_000_000), cfg)
    whale_filter.evaluate(event(margin=100_000), cfg)
    whale_filter.evaluate(event(margin=None), cfg)
    snapshot = whale_filter.stats.as_dict()
    assert snapshot["accepted"] == 1
    assert snapshot["rejected"] == {REASON_MARGIN: 1, REASON_MARGIN_UNKNOWN: 1}


# ── statistics ─────────────────────────────────────────────────

def test_filter_counts_acceptances_and_rejections_per_reason():
    whale_filter = make_filter()
    cfg = config(min_whale_value=2_000_000)
    whale_filter.evaluate(event(notional=3_000_000), cfg)
    whale_filter.evaluate(event(notional=100.0), cfg)
    whale_filter.evaluate(event(coin="DOGE"), config(coins=("BTC",), all_coins=False))
    snapshot = whale_filter.stats.as_dict()
    assert snapshot["accepted"] == 1
    assert snapshot["rejected"] == {REASON_THRESHOLD: 1, REASON_COIN: 1}


def test_filter_uses_its_settings_service_when_no_config_is_passed():
    whale_filter = make_filter(config(min_whale_value=50_000_000))
    assert whale_filter.evaluate(event(notional=3_000_000)).accepted is False
