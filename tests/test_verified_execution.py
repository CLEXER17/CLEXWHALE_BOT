"""Verified executions, position lifecycle and the order-alert split.

The named regression suite for the "VERIFIED EXECUTION + POSITION LIFECYCLE"
task (§31). Each test carries the number of the requirement it locks down, so a
failure names the guarantee that broke rather than the line that moved:

     1. a resting order never produces a trade alert
     2. a cancelled order never produces a trade alert
     3. a filled order does produce a trade alert
     4. a duplicate execution is suppressed
     5. a legitimate repeated execution is preserved
     6. a SELL execution is not automatically a SHORT
     7. a BUY execution is not automatically a LONG
     8. position opened
     9. position increased
    10. position reduced
    11. position closed
    12. a close reads its figures from the last non-zero snapshot
    13. ``/recent`` lists executions, not resting orders
    14. ``/whales`` counts executions
    15. the order-alert toggle works
    16. order *tracking* stays on while order *alerts* are off
    17. the trade threshold is applied to the executed value
    18. the coin filter works
    19. a complete wallet address survives every formatter
    20. the wallet is rendered in monospace
    21. order events and trade events have separate statistics
    22. main-admin permissions are intact
    23. co-admin permissions are intact
    24. an ordinary user cannot reach the admin controls

Requirements 25 (the persistence suite) and 26 (the pre-existing suite) are
statements about the whole test run, not about one module: they are satisfied by
``pytest`` passing as a whole and are recorded in ``.agent/TEST_STATUS.md``.

Almost nothing here is hand-built. Order events come out of
:class:`~app.whale.detector.WhaleDetector` fed with real ``orderUpdates`` shapes,
position events out of two ``clearinghouseState`` snapshots, and the accept/reject
decisions out of the live :class:`~app.whale.filters.WhaleFilter` reading the real
:class:`~app.services.settings_service.SettingsService`. Where the requirement is
about what a *user* sees, the assertion is made on the final Telegram text.
"""

from __future__ import annotations

from app.bot.handlers import data
from app.bot.handlers.callbacks import on_callback
from app.bot.keyboards import inline
from app.database.repository import EventRepository, PositionRepository
from app.services.settings_service import (
    KEY_MIN_ORDER,
    KEY_MIN_TRADE,
    KEY_ORDER_ALERTS,
    KEY_ORDERS,
)
from app.utils.formatting import utc_now, wallet_code
from app.whale.detector import WhaleDetector
from app.whale.events import EventType, ValueKind
from app.whale.filters import (
    REASON_COIN,
    REASON_DETECTOR,
    REASON_OK,
    REASON_ORDER_ALERTS_OFF,
    REASON_THRESHOLD,
)
from tests.conftest import (
    CO_ADMIN_ID,
    MAIN_ADMIN_ID,
    STRANGER_ID,
    FakeBot,
    FakeUpdate,
)
from tests.factories import (
    BTC_PX,
    WALLET_A,
    WALLET_B,
    make_context,
    make_order_state,
    make_order_update,
    make_position,
    make_trade,
    price_map,
)

PRICES = price_map()

#: A resting order of 40 BTC at $95,000 — the running example throughout. Its
#: face value is $3.80M, which is above the $2M test threshold, so every rejection
#: below is about *what the event is*, never about its size.
ORDER_PX = 95_000.0
ORDER_SZ = 40.0
ORDER_NOTIONAL = ORDER_PX * ORDER_SZ


def detector() -> WhaleDetector:
    return WhaleDetector(lambda coin: PRICES.get(coin))


# ── event builders, all through production code ────────────────

def placed_order():
    """``ORDER_PLACED``: an order that is merely sitting on the book."""
    event = detector().from_order_update(
        WALLET_A, make_order_update(sz=ORDER_SZ, limit_px=ORDER_PX, status="open")
    )
    assert event is not None and event.event_type is EventType.ORDER_PLACED
    return event


def cancelled_order():
    """``ORDER_CANCELLED``: the same order pulled. Nothing traded."""
    event = detector().from_order_update(
        WALLET_A,
        make_order_update(sz=ORDER_SZ, limit_px=ORDER_PX, status="canceled"),
        make_order_state(size=ORDER_SZ, limit_px=ORDER_PX),
    )
    assert event is not None and event.event_type is EventType.ORDER_CANCELLED
    return event


def filled_order(*, sz: float = ORDER_SZ, orig_sz: float | None = None):
    """``ORDER_FILLED``: the order executed. Hyperliquid reports a completed
    order with ``sz == 0``, so the executed size comes from the state that was
    still resting a moment earlier."""
    event = detector().from_order_update(
        WALLET_A,
        make_order_update(sz=0.0, orig_sz=orig_sz or sz, limit_px=ORDER_PX, status="filled"),
        make_order_state(size=sz, orig_size=orig_sz or sz, limit_px=ORDER_PX),
    )
    assert event is not None and event.event_type is EventType.ORDER_FILLED
    return event


async def evaluate(container, event):
    """The live filter's verdict on one event, with the current settings."""
    return container.engine.filter.evaluate(event)


async def deliver(container, event) -> list[str]:
    """Push one event through the engine and return what Telegram received.

    Everything from the filter onwards is the production path: threshold and
    coin gates, both duplicate gates, the ``whale_events`` write, then
    :meth:`AlertService.enqueue` rendering and the sender task.
    """
    bot = FakeBot()
    container.alerts.attach_bot(bot)
    await container.alerts.start()
    await container.engine._emit(event)
    # ``enqueue`` runs inside ``_emit``, so by now the job is either on the queue
    # or was never accepted; joining is exact either way and needs no polling.
    await container.alerts._queue.join()
    return [message["text"] for message in bot.messages]


# ══ 1-3, 15-18: an execution is not an intention ═══════════════

async def test_1_a_resting_order_produces_no_trade_alert(container):
    """§31 (1). The reported defect in its original form: an order sitting on the
    book was announced as though the whale had traded. Two independent guards —
    the filter refuses to publish it at all, and even if it were published the
    text says in words that nothing executed."""
    event = placed_order()

    verdict = await evaluate(container, event)
    assert verdict.accepted is False
    assert verdict.reason == REASON_ORDER_ALERTS_OFF

    rendered = container.alerts.render(event)
    assert "WHALE TRADE" not in rendered
    assert "🐋 LARGE LIMIT ORDER" in rendered
    assert "🔎 <b>RESTING ORDER — NOT AN EXECUTION</b>" in rendered
    assert "💰 <b>Executed:</b>" not in rendered
    assert event.is_execution is False
    assert event.value_kind is ValueKind.ORDER_NOTIONAL


async def test_2_a_cancelled_order_produces_no_trade_alert(container):
    """§31 (2). "Whale sold $6.83M" for an order that was merely withdrawn was
    the worst version of the defect: the money never moved."""
    event = cancelled_order()

    verdict = await evaluate(container, event)
    assert verdict.accepted is False
    assert verdict.reason == REASON_ORDER_ALERTS_OFF

    rendered = container.alerts.render(event)
    assert "WHALE TRADE" not in rendered
    assert "🚨 WHALE ORDER CANCELLED" in rendered
    assert "🔎 <b>ORDER CANCELLED — NOTHING WAS TRADED</b>" in rendered
    assert event.is_execution is False


async def test_3_a_filled_order_produces_a_trade_alert(container):
    """§31 (3). The half of the fix that is easy to lose: silencing intentions
    must not silence the real thing. A fill is an execution, so it reaches the
    feed with order alerts off, and it is measured as a trade."""
    event = filled_order()
    assert event.is_execution is True
    assert event.value_kind is ValueKind.TRADE_VALUE
    assert event.threshold_class == "trade"
    assert event.notional == ORDER_NOTIONAL

    assert container.settings.config.enable_order_alerts is False
    verdict = await evaluate(container, event)
    assert verdict.accepted is True
    assert verdict.reason == REASON_OK

    sent = await deliver(container, event)
    assert len(sent) == 1
    assert "🐋 WHALE TRADE — ORDER FILLED" in sent[0]
    assert "🔎 <b>VERIFIED EXECUTION</b>" in sent[0]
    assert "💰 <b>Executed:</b>" in sent[0]
    assert "🐋 CLEXER WHALE MONITOR" in sent[0]


async def test_15_the_order_alert_toggle_publishes_resting_orders(container):
    """§31 (15). Off by default, and an administrator who asks for order alerts
    gets them — the switch has to work in both directions."""
    event = placed_order()
    assert (await evaluate(container, event)).reason == REASON_ORDER_ALERTS_OFF

    await container.settings.toggle(KEY_ORDER_ALERTS, MAIN_ADMIN_ID)
    assert container.settings.config.enable_order_alerts is True
    assert (await evaluate(container, placed_order())).accepted is True

    await container.settings.toggle(KEY_ORDER_ALERTS, MAIN_ADMIN_ID)
    assert (await evaluate(container, placed_order())).reason == REASON_ORDER_ALERTS_OFF


async def test_16_order_tracking_runs_while_order_alerts_are_off(container):
    """§31 (16). The two settings mean different things. Tracking is where fills,
    TP/SL and order attribution come from, so it is on by default; publishing
    resting orders is a separate, off-by-default choice."""
    config = container.settings.config
    assert config.enable_order_detector is True, "internal order tracking must stay on"
    assert config.enable_order_alerts is False, "order alerts must default to off"
    assert config.detector_enabled("order") is True

    # Detection ran (the events exist at all), the fill publishes, the
    # intention does not — and the reason distinguishes "not alerted" from
    # "not detected".
    assert (await evaluate(container, filled_order())).accepted is True
    assert (await evaluate(container, placed_order())).reason == REASON_ORDER_ALERTS_OFF

    # Turning tracking off is a different act with a different reason, and it
    # silences the fill too — which is why the panel does not flip this switch.
    await container.settings.set_value(KEY_ORDERS, False, MAIN_ADMIN_ID)
    verdict = await evaluate(container, filled_order())
    assert verdict.accepted is False
    assert verdict.reason == REASON_DETECTOR


async def test_17_the_trade_threshold_is_applied_to_the_executed_value(container):
    """§31 (17). A fill is gated on what actually changed hands, not on the face
    value of the order it belonged to and not on the resting-order threshold."""
    await container.settings.set_value(KEY_MIN_TRADE, 5_000_000.0, MAIN_ADMIN_ID)
    await container.settings.set_value(KEY_MIN_ORDER, 1_000_000.0, MAIN_ADMIN_ID)

    # $3.80M executed out of a $12.35M order. The order-sized figure would pass
    # the $1M order gate; the executed figure must be judged at $5M.
    event = filled_order(sz=ORDER_SZ, orig_sz=130.0)
    assert event.notional == ORDER_NOTIONAL
    assert event.numeric("orig_notional") == 130.0 * ORDER_PX
    verdict = await evaluate(container, event)
    assert verdict.accepted is False
    assert verdict.reason == REASON_THRESHOLD
    assert verdict.threshold == 5_000_000.0

    # The same event once the executed value clears the trade gate.
    bigger = filled_order(sz=60.0, orig_sz=130.0)
    assert bigger.notional == 60.0 * ORDER_PX
    assert (await evaluate(container, bigger)).accepted is True


async def test_18_the_coin_filter_applies_to_executions(container):
    """§31 (18). ``BTC,ETH,SOL`` is the test slate; DOGE is not on it."""
    doge = detector().from_order_update(
        WALLET_A,
        make_order_update(coin="DOGE", sz=0.0, orig_sz=ORDER_SZ, limit_px=ORDER_PX, status="filled"),
        make_order_state(coin="DOGE", size=ORDER_SZ, limit_px=ORDER_PX),
    )
    assert doge is not None
    verdict = await evaluate(container, doge)
    assert verdict.accepted is False
    assert verdict.reason == REASON_COIN

    await container.settings.add_coins(["DOGE"], MAIN_ADMIN_ID)
    assert (await evaluate(container, doge)).accepted is True


# ══ 4-5: deduplication, without over-deduplicating ═════════════

async def test_4_the_same_execution_is_alerted_once(container):
    """§31 (4). One fill, one alert — however many times the feed replays it.
    A websocket reconnect re-sends its snapshot, so this is the normal case."""
    trade = make_trade(sz=50.0, tid=4_242_424)
    first = detector().from_trade(trade, wallet=WALLET_A)
    again = detector().from_trade(trade, wallet=WALLET_A)
    assert first is not None and again is not None

    sent = await deliver(container, first)
    assert len(sent) == 1
    before = container.engine.duplicates_by_category.get("execution", 0)

    await container.engine._emit(again)
    assert container.engine.duplicates_by_category.get("execution", 0) == before + 1

    async with container.db.session() as session:
        rows = await EventRepository.recent(session, limit=10)
    assert len(rows) == 1, "the duplicate reached the database"


async def test_5_a_second_genuine_execution_is_not_swallowed(container):
    """§31 (5). "Do not over-deduplicate": a whale that trades twice has traded
    twice. The two fills differ only in the exchange's own trade id, which is
    exactly what distinguishes a repeat from a replay."""
    await container.settings.set_cooldown(0, MAIN_ADMIN_ID)

    first = detector().from_trade(make_trade(sz=50.0, tid=7_000_001), wallet=WALLET_A)
    second = detector().from_trade(make_trade(sz=50.0, tid=7_000_002), wallet=WALLET_A)
    assert first is not None and second is not None
    assert first.notional == second.notional, "identical in every way but identity"

    await container.engine._emit(first)
    await container.engine._emit(second)

    async with container.db.session() as session:
        rows = await EventRepository.recent(session, limit=10)
    assert len(rows) == 2
    assert {row.dedup_key for row in rows} == {first.dedup_key, second.dedup_key}


async def test_5b_a_repeated_position_change_stays_possible(container):
    """§31 (5). The same reasoning for position state, which is why
    ``whale_events.dedup_key`` carries no UNIQUE constraint: a whale may add the
    same size twice, and the second add is news, not a duplicate."""
    await container.settings.set_cooldown(0, MAIN_ADMIN_ID)
    build = detector().from_position_change

    first = build(WALLET_A, "BTC", make_position(szi=60.0), make_position(szi=90.0))
    second = build(WALLET_A, "BTC", make_position(szi=90.0), make_position(szi=120.0))
    assert first is not None and second is not None
    assert first.notional == second.notional, "two identical $3M adds"

    await container.engine._emit(first)
    await container.engine._emit(second)

    async with container.db.session() as session:
        rows = await EventRepository.recent(session, limit=10)
    assert len(rows) == 2, "a legitimate repeated position change was suppressed"


# ══ 6-12: position lifecycle from verified state ═══════════════

async def test_8_a_new_position_is_opened(container):
    """§31 (8)."""
    event = detector().from_position_change(WALLET_A, "BTC", None, make_position(szi=60.0))
    assert event is not None
    assert event.event_type is EventType.POSITION_OPENED
    assert event.side == "LONG"
    assert event.value_kind is ValueKind.POSITION_DELTA


async def test_9_a_position_is_increased_by_its_delta(container):
    """§31 (9). The figure is the size that was added, not the whole position:
    a $3M add to a $6M long is a $3M event."""
    event = detector().from_position_change(
        WALLET_A, "BTC", make_position(szi=60.0), make_position(szi=90.0)
    )
    assert event is not None
    assert event.event_type is EventType.POSITION_INCREASED
    assert event.side == "LONG"
    assert event.notional == 30.0 * BTC_PX


async def test_10_a_sell_that_trims_a_long_reduces_it_and_stays_long(container):
    """§31 (6) and (10) together, which is where the confusion lives: the
    execution that reduced this position was a SELL, and the position is still a
    LONG. The side comes from the verified snapshot, never from the fill."""
    before, after = make_position(szi=60.0), make_position(szi=30.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, after)
    assert event is not None
    assert event.event_type is EventType.POSITION_DECREASED
    assert event.side == "LONG", "a SELL that trims a long does not make it a short"
    assert event.notional == 30.0 * BTC_PX

    # The execution itself: SELL is the book side, and the position it belongs
    # to is reported separately and still reads LONG.
    sell = detector().from_trade(
        make_trade(side="A", buyer=WALLET_B, seller=WALLET_A, sz=30.0),
        wallet=WALLET_A,
        context=make_context(position=after),
    )
    assert sell is not None
    assert sell.side == "SELL"
    assert sell.value("position_side") == "LONG"

    rendered = container.alerts.render(sell)
    assert "🔴 SELL" in rendered
    assert "SHORT" not in rendered


async def test_7_a_buy_that_trims_a_short_reduces_it_and_stays_short(container):
    """§31 (7). The mirror image: a BUY is not evidence of a long."""
    before, after = make_position(szi=-60.0), make_position(szi=-30.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, after)
    assert event is not None
    assert event.event_type is EventType.POSITION_DECREASED
    assert event.side == "SHORT", "a BUY that trims a short does not make it a long"

    buy = detector().from_trade(
        make_trade(side="B", buyer=WALLET_A, seller=WALLET_B, sz=30.0),
        wallet=WALLET_A,
        context=make_context(position=after),
    )
    assert buy is not None
    assert buy.side == "BUY"
    assert buy.value("position_side") == "SHORT"

    rendered = container.alerts.render(buy)
    assert "🟢 BUY" in rendered
    assert "LONG" not in rendered


async def test_6b_an_execution_side_never_writes_a_position_row(container):
    """§31 (6). The same rule one layer down. ``positions.side`` may only hold a
    LONG/SHORT read off a ``clearinghouseState`` snapshot, so a SELL execution
    with nothing behind it writes no position row at all rather than a fabricated
    SHORT — and a SELL that *does* carry a verified snapshot writes the side the
    snapshot states, which is LONG."""
    bare = detector().from_trade(
        make_trade(side="A", buyer=WALLET_B, seller=WALLET_A, sz=50.0, tid=6_060_005),
        wallet=WALLET_A,
    )
    assert bare is not None and bare.side == "SELL"
    assert bare.has("position_side") is False, "no snapshot, so no position side"

    await container.engine._emit(bare)

    async with container.db.session() as session:
        assert await PositionRepository.get(session, WALLET_A, "BTC") is None

    # The same execution with a verified LONG snapshot behind it. The row records
    # the position's side, never the fill's.
    await container.settings.set_cooldown(0, MAIN_ADMIN_ID)
    verified = detector().from_trade(
        make_trade(side="A", buyer=WALLET_B, seller=WALLET_A, sz=30.0, tid=6_060_006),
        wallet=WALLET_A,
        context=make_context(position=make_position(szi=30.0)),
    )
    assert verified is not None and verified.side == "SELL"
    assert verified.value("position_side") == "LONG"

    await container.engine._emit(verified)

    async with container.db.session() as session:
        row = await PositionRepository.get(session, WALLET_A, "BTC")
    assert row is not None, "a verified snapshot must be persisted"
    assert row.side == "LONG", "the fill's SELL was written as the position side"


async def test_11_a_position_that_reaches_zero_is_closed(container):
    """§31 (11). Measured by the notional that left the book, not by a delta."""
    before = make_position(szi=60.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, None)
    assert event is not None
    assert event.event_type is EventType.POSITION_CLOSED
    assert event.side == "LONG"
    assert event.value_kind is ValueKind.POSITION_NOTIONAL
    assert event.notional == before.notional
    assert event.numeric("closed_position_value") == before.notional


async def test_12_a_close_reads_its_figures_from_the_last_non_zero_snapshot(container):
    """§31 (12). Hyperliquid drops a closed position from ``assetPositions``, but
    a snapshot taken mid-close can still arrive with ``szi == 0`` while carrying
    the entry price, leverage and liquidation price of the position that has just
    gone. Reading a side off that snapshot labelled every closed LONG a SHORT;
    reading its numbers reported a position the trader no longer holds."""
    before = make_position(szi=60.0, entry_px=98_000.0, unrealized_pnl=120_000.0)
    closing = make_position(szi=0.0, entry_px=98_000.0, liquidation_px=74_500.0)
    assert closing.side is None, "a flat snapshot has no side to read"
    assert closing.is_flat is True

    event = detector().from_position_change(
        WALLET_A, "BTC", before, closing, context=make_context(position=closing)
    )
    assert event is not None
    assert event.event_type is EventType.POSITION_CLOSED
    assert event.side == "LONG", "the closed position was a long"

    # Information that disappears with the position comes from the pre-close
    # snapshot, and nothing else is reconstructed.
    assert event.numeric("closed_position_value") == before.notional
    assert event.point("final_unrealized_pnl").value == 120_000.0
    assert event.point("entry_px").available is False
    assert event.point("leverage").available is False
    assert event.point("liquidation_px").available is False

    rendered = container.alerts.render(event)
    assert "🐋 WHALE POSITION CLOSED" in rendered
    assert "📈 LONG" in rendered
    assert "SHORT" not in rendered
    assert "🔎 <b>VERIFIED POSITION CLOSURE</b>" in rendered
    # An estimate is labelled as one: the close may have printed at another
    # price and fees are not included.
    assert "Final PnL (est.):" in rendered


# ══ 13, 14, 21: the history and statistics commands ════════════

async def seed_history(database) -> None:
    """One of each kind of row, on a different coin so the assertions can tell
    them apart in the rendered list."""
    rows = [
        ("WHALE_TRADE", "BTC", "BUY", "TRADE_VALUE", 3_000_000.0, "x:trade"),
        ("ORDER_FILLED", "ETH", "SELL", "TRADE_VALUE", 4_000_000.0, "x:fill"),
        ("POSITION_OPENED", "SOL", "LONG", "POSITION_DELTA", 5_000_000.0, "x:position"),
        ("ORDER_PLACED", "BTC", "BUY", "ORDER_NOTIONAL", 51_000_000.0, "x:placed"),
        ("ORDER_CANCELLED", "ETH", "SELL", "ORDER_NOTIONAL", 62_000_000.0, "x:cancelled"),
        ("ORDER_MODIFIED", "SOL", "BUY", "ORDER_NOTIONAL", 73_000_000.0, "x:modified"),
    ]
    async with database.session() as session:
        for event_type, coin, side, kind, notional, key in rows:
            await EventRepository.insert(
                session,
                event_type=event_type,
                coin=coin,
                side=side,
                wallet=WALLET_A.lower(),
                notional=notional,
                value_kind=kind,
                detail={},
                dedup_key=key,
                event_time=utc_now(),
            )


async def test_13_recent_lists_executions_and_not_resting_orders(container, ctx, database):
    """§31 (13). ``/recent`` is a history of things that happened. The three
    order-book intentions are the three largest rows in the table, so if the
    command were unfiltered they would be impossible to miss."""
    await seed_history(database)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/recent")
    await data.cmd_recent(update, ctx)
    body = update.last.split("<b>Latest</b>")[-1]

    assert "$3.00M" in body, "the executed trade is missing"
    assert "$4.00M" in body, "the filled order is missing"
    assert "$5.00M" in body, "the position change is missing"
    for intention in ("$51.00M", "$62.00M", "$73.00M"):
        assert intention not in body, f"a resting order reached /recent ({intention})"

    # And with order monitoring explicitly enabled they are visible again.
    await container.settings.toggle(KEY_ORDER_ALERTS, MAIN_ADMIN_ID)
    opted_in = FakeUpdate(MAIN_ADMIN_ID, text="/recent")
    await data.cmd_recent(opted_in, ctx)
    assert "$73.00M" in opted_in.last.split("<b>Latest</b>")[-1]


async def test_13b_whales_lists_executions_and_not_resting_orders(container, ctx, database):
    """§31 (13). ``/whales`` shares the filter, because the two commands answer
    the same question over different windows."""
    await seed_history(database)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/whales")
    await data.cmd_whales(update, ctx)

    assert "$3.00M" in update.last
    for intention in ("$51.00M", "$62.00M", "$73.00M"):
        assert intention not in update.last


async def test_14_the_wallet_leaderboard_counts_executions(container, ctx, database):
    """§31 (14). "Most active" means trades. A wallet that placed, modified and
    cancelled an order has traded twice here, not five times — the persisted
    ``wallets.event_count`` counts every row, which is why the view does not use
    it."""
    await seed_history(database)
    assert container.engine.tracker.top(limit=6) == [], "the fallback path is the one under test"

    update = FakeUpdate(MAIN_ADMIN_ID, text="/wallets")
    await data.cmd_wallets(update, ctx)

    assert f"{wallet_code(WALLET_A)} — 2 trades" in update.last
    assert "$7.00M" in update.last, "volume must be the executed notional"


async def test_21_executions_and_order_events_are_counted_apart(container, ctx, database):
    """§31 (21). Three numbers, three labels. Reporting 411 order events as
    "trades" is what made a quiet market look like a busy one."""
    await seed_history(database)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/stats")
    await data.cmd_stats(update, ctx)
    text = update.last

    assert "<b>Executed Trades:</b> 2" in text
    assert "<b>Trade Notional:</b> $7.00M" in text
    assert "<b>Position Events:</b> 1" in text
    assert "<b>Order Events:</b> 3" in text
    assert "<b>Events Recorded:</b> 6" in text
    # The $186M of intended value is reported on the order line and nowhere else:
    # it belongs to three orders, none of which moved a coin.
    assert "$186.00M intended" in text
    assert "<b>Trade Notional:</b> $186.00M" not in text
    assert "<b>Largest Trade:</b> $4.00M" in text


# ══ 19-20: the wallet address, in full, everywhere ═════════════

async def test_19_the_complete_address_survives_every_formatter(container, ctx, database):
    """§31 (19). 42 characters, in the alert and in every list view. A prefix and
    a suffix are not an address: they cannot be pasted into a block explorer and
    two whales can share them."""
    await seed_history(database)
    texts = [container.alerts.render(filled_order())]
    for handler in (data.cmd_recent, data.cmd_whales, data.cmd_wallets):
        update = FakeUpdate(MAIN_ADMIN_ID, text="/x")
        await handler(update, ctx)
        texts.append(update.last)

    full = WALLET_A.lower()
    assert full in texts[0], "the alert dropped the address"
    assert full in texts[2] and full in texts[3]
    for text in texts:
        assert f"{full[:6]}..." not in text
        assert f"{full[:6]}…" not in text

    async with container.db.session() as session:
        stored = await EventRepository.recent(session, limit=1)
    assert stored[0].wallet == full and len(stored[0].wallet) == 42


async def test_20_the_wallet_is_rendered_in_monospace(container, ctx, database):
    """§31 (20). Monospace is what makes Telegram offer tap-to-copy; bold is not
    a substitute. In the alert it also sits on its own line, because a wrapped
    address reads as a truncated one."""
    await seed_history(database)
    full = WALLET_A.lower()

    rendered = container.alerts.render(filled_order())
    assert f"\n<code>{full}</code>" in rendered

    update = FakeUpdate(MAIN_ADMIN_ID, text="/whales")
    await data.cmd_whales(update, ctx)
    assert f"<code>{full}</code>" in update.last
    assert f"<b>{full}</b>" not in update.last


async def test_20b_no_callback_payload_carries_a_wallet(container):
    """§31 (20)/Task G. Telegram caps ``callback_data`` at 64 bytes, which is the
    pressure that produces truncated addresses. No panel puts an address in a
    payload at all, so the constraint never reaches the display: the views read
    the complete value out of the database."""
    config = container.settings.config
    keyboards = [
        inline.control_panel(config),
        inline.alert_settings_panel(config),
        inline.settings_panel(config),
        inline.data_panel("wallets", admin=True),
        inline.data_panel("whales", admin=True),
        inline.monitoring_controls(config),
    ]
    payloads = [
        button.callback_data
        for keyboard in keyboards
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert payloads
    for payload in payloads:
        assert "0x" not in payload.lower(), f"a wallet-shaped payload: {payload}"
        assert len(payload.encode()) <= 64, f"payload over Telegram's limit: {payload}"


# ══ 22-24: the admin system is untouched ══════════════════════

async def test_22_the_main_admin_can_flip_the_order_alert_switch(container, ctx):
    """§31 (22)."""
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data=f"set:toggle:{KEY_ORDER_ALERTS}")
    await on_callback(update, ctx)

    assert update.callback_query.edits, "the panel did not re-render"
    assert update.callback_query.alerts == []
    assert container.settings.config.enable_order_alerts is True


async def test_23_a_co_admin_can_flip_it_too(container, ctx):
    """§31 (23). A co-admin holds CHANGE_SETTINGS; only admin management, the
    audit log and the settings reset are main-admin-only."""
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")

    update = FakeUpdate(CO_ADMIN_ID, callback_data=f"set:toggle:{KEY_ORDER_ALERTS}")
    await on_callback(update, ctx)

    assert update.callback_query.edits
    assert update.callback_query.alerts == []
    assert container.settings.config.enable_order_alerts is True


async def test_24_an_ordinary_user_cannot_flip_it(container, ctx):
    """§31 (24). Authority is re-derived from ``effective_user.id`` on every
    callback, so a hand-crafted payload changes nothing: the id is the only
    input that counts, and it is not on the roster."""
    before = container.settings.config.enable_order_alerts

    update = FakeUpdate(STRANGER_ID, callback_data=f"set:toggle:{KEY_ORDER_ALERTS}")
    await on_callback(update, ctx)

    assert update.callback_query.edits == [], "an unauthorised user saw a panel"
    assert update.callback_query.alerts, "the refusal was silent"
    assert container.settings.config.enable_order_alerts is before


async def test_24b_an_unlisted_switch_is_refused_even_for_the_main_admin(container, ctx):
    """§31 (24). The panel's toggle route carries a key from the payload, so it
    is whitelisted: only the alert booleans may be flipped through it. Monitoring
    and public mode have their own capabilities and their own panels."""
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="set:toggle:monitoring_enabled")
    await on_callback(update, ctx)

    assert update.callback_query.alerts
    assert container.settings.config.monitoring_enabled is True
