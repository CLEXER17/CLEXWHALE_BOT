"""Duplicate prevention.

Spec §36: "Duplicate prevention". Spec §19: the same whale event must not be
announced twice, but a genuine escalation must still get through.
"""

from __future__ import annotations

from app.utils.formatting import DataPoint
from app.whale.dedup import Deduplicator, cooldown_key, identity_key, magnitude_bucket
from app.whale.events import EventType, ValueKind, WhaleEvent
from tests.factories import WALLET_A, ago


def trade_event(*, tid: int | None = 1, notional: float = 3_000_000.0, coin: str = "BTC") -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.WHALE_TRADE,
        coin=coin,
        notional=notional,
        value_kind=ValueKind.TRADE_VALUE,
        side="LONG",
        wallet=WALLET_A,
        detection="Large market trade",
        context={"tid": tid},
    )
    event.set("price", DataPoint.confirmed(100_000.0))
    event.set("size", DataPoint.confirmed(notional / 100_000.0))
    return event


def order_event(*, oid: int = 555, status: str = "open", size: float = 40.0) -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.ORDER_PLACED,
        coin="BTC",
        notional=95_000.0 * size,
        value_kind=ValueKind.ORDER_NOTIONAL,
        side="BUY",
        wallet=WALLET_A,
        detection="Large limit order placed",
        order_id=oid,
        status=status,
    )
    event.set("size", DataPoint.confirmed(size))
    return event


def position_event(*, size: float = 60.0, position_value: float = 6_000_000.0) -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.POSITION_OPENED,
        coin="BTC",
        notional=position_value,
        value_kind=ValueKind.POSITION_DELTA,
        side="LONG",
        wallet=WALLET_A,
        detection="Position opened",
    )
    event.set("size", DataPoint.confirmed(size))
    event.set("position_value", DataPoint.confirmed(position_value))
    event.set("entry_px", DataPoint.confirmed(98_000.0))
    return event


# ── identity gate ──────────────────────────────────────────────

def test_first_event_passes():
    assert Deduplicator().check(trade_event(), cooldown_seconds=0) is True


def test_the_same_trade_id_is_suppressed():
    dedup = Deduplicator()
    assert dedup.check(trade_event(tid=42), cooldown_seconds=0) is True
    assert dedup.check(trade_event(tid=42), cooldown_seconds=0) is False
    assert dedup.stats.duplicate_identity == 1


def test_different_trade_ids_both_pass():
    dedup = Deduplicator()
    assert dedup.check(trade_event(tid=1), cooldown_seconds=0) is True
    assert dedup.check(trade_event(tid=2), cooldown_seconds=0) is True


def test_check_assigns_the_dedup_key():
    event = trade_event(tid=7)
    Deduplicator().check(event, cooldown_seconds=0)
    assert event.dedup_key
    assert event.dedup_key == identity_key(event)
    assert event.db_fields()["dedup_key"] == event.dedup_key


def test_order_identity_includes_the_status():
    """The same oid moving open → cancelled is two distinct events."""
    dedup = Deduplicator()
    assert dedup.check(order_event(oid=9, status="open"), cooldown_seconds=0) is True
    assert dedup.check(order_event(oid=9, status="open"), cooldown_seconds=0) is False
    assert dedup.check(order_event(oid=9, status="canceled"), cooldown_seconds=0) is True


def test_order_identity_includes_the_remaining_size():
    """Successive partial fills of one order are distinct observations."""
    dedup = Deduplicator()
    assert dedup.check(order_event(oid=9, size=40.0), cooldown_seconds=0) is True
    assert dedup.check(order_event(oid=9, size=25.0), cooldown_seconds=0) is True


def test_identical_position_snapshot_is_suppressed():
    """Two polls that read the same state must not alert twice."""
    dedup = Deduplicator()
    assert dedup.check(position_event(), cooldown_seconds=0) is True
    assert dedup.check(position_event(), cooldown_seconds=0) is False


def test_different_wallets_do_not_collide():
    dedup = Deduplicator()
    first = trade_event(tid=None)
    second = trade_event(tid=None)
    second.wallet = "0x" + "99" * 20
    assert dedup.check(first, cooldown_seconds=0) is True
    assert dedup.check(second, cooldown_seconds=0) is True


def test_different_coins_do_not_collide():
    dedup = Deduplicator()
    assert dedup.check(trade_event(tid=None, coin="BTC"), cooldown_seconds=0) is True
    assert dedup.check(trade_event(tid=None, coin="ETH"), cooldown_seconds=0) is True


# ── cooldown gate ──────────────────────────────────────────────

def test_second_similar_event_is_cooled_down():
    dedup = Deduplicator()
    assert dedup.check(trade_event(tid=1), cooldown_seconds=30) is True
    assert dedup.check(trade_event(tid=2), cooldown_seconds=30) is False
    assert dedup.stats.cooled_down == 1


def test_cooldown_of_zero_disables_the_second_gate():
    dedup = Deduplicator()
    assert dedup.check(trade_event(tid=1), cooldown_seconds=0) is True
    assert dedup.check(trade_event(tid=2), cooldown_seconds=0) is True


def test_a_ten_times_larger_follow_up_escapes_the_cooldown():
    """§19: a $2M nudge is noise, a $200M move is news."""
    dedup = Deduplicator()
    assert dedup.check(trade_event(tid=1, notional=3_000_000), cooldown_seconds=30) is True
    assert dedup.check(trade_event(tid=2, notional=3_500_000), cooldown_seconds=30) is False
    assert dedup.check(trade_event(tid=3, notional=45_000_000), cooldown_seconds=30) is True


def test_a_cooled_down_event_stays_suppressed_on_retry():
    """Being cooled down still records the identity, so retries stay quiet."""
    dedup = Deduplicator()
    dedup.check(trade_event(tid=1), cooldown_seconds=30)
    repeat = trade_event(tid=2)
    assert dedup.check(repeat, cooldown_seconds=30) is False
    assert dedup.check(trade_event(tid=2), cooldown_seconds=30) is False
    assert dedup.stats.duplicate_identity == 1


def test_magnitude_buckets():
    assert magnitude_bucket(0.0) == 0
    assert magnitude_bucket(999.0) == 2
    assert magnitude_bucket(2_000_000.0) == 6
    assert magnitude_bucket(20_000_000.0) == 7
    assert magnitude_bucket(-20_000_000.0) == 7


def test_cooldown_key_ignores_small_notional_changes():
    a = trade_event(tid=1, notional=3_000_000)
    b = trade_event(tid=2, notional=9_000_000)
    assert cooldown_key(a) == cooldown_key(b)


def test_cooldown_key_separates_event_types():
    assert cooldown_key(trade_event()) != cooldown_key(order_event())


# ── recovery ───────────────────────────────────────────────────

def test_forget_allows_a_failed_alert_to_be_retried():
    dedup = Deduplicator()
    event = trade_event(tid=1)
    assert dedup.check(event, cooldown_seconds=0) is True
    dedup.forget(event)
    assert dedup.check(trade_event(tid=1), cooldown_seconds=0) is True


def test_warm_suppresses_events_already_alerted_before_a_restart():
    """§48: a redeploy must not re-announce what was live at the time."""
    event = trade_event(tid=99)
    key = identity_key(event)
    dedup = Deduplicator()
    assert dedup.warm([key]) == 1
    assert dedup.check(trade_event(tid=99), cooldown_seconds=0) is False


def test_identity_entries_expire():
    import time

    dedup = Deduplicator(identity_ttl=0.01)
    assert dedup.check(trade_event(tid=1), cooldown_seconds=0) is True
    time.sleep(0.05)
    assert dedup.check(trade_event(tid=1), cooldown_seconds=0) is True


def test_stats_snapshot_shape():
    dedup = Deduplicator()
    dedup.check(trade_event(tid=1), cooldown_seconds=0)
    dedup.check(trade_event(tid=1), cooldown_seconds=0)
    snapshot = dedup.as_dict()
    assert snapshot["checked"] == 2
    assert snapshot["passed"] == 1
    assert snapshot["duplicate_identity"] == 1
    assert snapshot["identity_cached"] >= 1


def test_anonymous_book_events_do_not_collide_with_wallet_events():
    book = WhaleEvent(
        event_type=EventType.BOOK_LEVEL,
        coin="BTC",
        notional=4_000_000.0,
        value_kind=ValueKind.BOOK_LEVEL_NOTIONAL,
        side="BUY",
        wallet=None,
        detection="Large aggregate book level",
        event_time=ago(1),
    )
    book.set("price", DataPoint.confirmed(99_000.0))
    dedup = Deduplicator()
    assert dedup.check(book, cooldown_seconds=0) is True
    assert dedup.check(book, cooldown_seconds=0) is False
