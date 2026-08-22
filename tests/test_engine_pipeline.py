"""End-to-end pipeline, entirely offline.

One raw Hyperliquid ``trades`` frame is pushed into the websocket handler and the
assertions are made on the *final Telegram text* and the rows left in the
database. Everything between is the real production path:

    frame -> HyperliquidWebSocket._dispatch -> WhaleEngine._on_market_message
          -> HyperliquidParser.parse_trades -> _on_trade (threshold + coin gate)
          -> queue -> _process_trade -> _enrich (REST) -> WhaleTracker
          -> WhaleDetector.from_trade -> WhaleFilter -> Deduplicator
          -> _persist -> AlertService.enqueue -> AlertFormatter -> bot.send_message

Nothing in that chain is stubbed. Only the two network seams are replaced, the
same two used by ``tests/test_resilience.py``: the module-level
``ws_connect`` factory and ``HyperliquidREST._client``. The "Telegram API" is
``conftest.FakeBot``, which records what would have been sent.

The negative cases matter as much as the positive one. A sub-threshold trade, a
coin outside the filter, a repeated trade id and a paused monitor must all
produce *no* message and *no* row — and where Hyperliquid does not supply TP, SL
or a position, the alert must say so rather than invent a number (spec §1, §34).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.database.repository import EventRepository, PositionRepository, WalletRepository
from app.hyperliquid import websocket as ws_module
from app.whale.events import EventType, ValueKind
from tests.conftest import MAIN_ADMIN_ID, FakeBot
from tests.factories import (
    BTC_PX,
    WALLET_A,
    WALLET_B,
    make_liquidation_fill,
    raw_trade,
    raw_user_events,
)
from tests.test_resilience import (
    META_AND_CTXS,
    FakeEndpoint,
    FakeResponse,
    FakeSocket,
    UniverseHTTP,
    wait_for,
)

DIVIDER = "━━━━━━━━━━━━━━━━━━"

#: A real ``clearinghouseState`` body: one 60 BTC cross long at 5x.
BTC_LONG = {
    "marginSummary": {
        "accountValue": "12000000.0",
        "totalNtlPos": "6000000.0",
        "totalMarginUsed": "1176000.0",
    },
    "withdrawable": "6000000.0",
    "time": 1_700_000_000_000,
    "assetPositions": [
        {
            "type": "oneWay",
            "position": {
                "coin": "BTC",
                "szi": "60.0",
                "entryPx": "98000.0",
                "positionValue": "6000000.0",
                "unrealizedPnl": "120000.0",
                "returnOnEquity": "0.10",
                "liquidationPx": "74500.0",
                "marginUsed": "1176000.0",
                "maxLeverage": 40,
                "leverage": {"type": "cross", "value": 5},
                "cumFunding": {"sinceOpen": "-1500.0"},
            },
        }
    ],
}

#: A real ``frontendOpenOrders`` body: a trader-set TP and SL on that position.
BTC_TRIGGERS = [
    {
        "coin": "BTC",
        "oid": 9_500_001,
        "side": "A",
        "limitPx": "115000.0",
        "sz": "60.0",
        "origSz": "60.0",
        "timestamp": 1_700_000_000_000,
        "orderType": "Take Profit Market",
        "reduceOnly": True,
        "isTrigger": True,
        "triggerPx": "115000.0",
        "triggerCondition": "tp",
        "isPositionTpsl": True,
        "tif": None,
    },
    {
        "coin": "BTC",
        "oid": 9_500_002,
        "side": "A",
        "limitPx": "88000.0",
        "sz": "60.0",
        "origSz": "60.0",
        "timestamp": 1_700_000_000_000,
        "orderType": "Stop Market",
        "reduceOnly": True,
        "isTrigger": True,
        "triggerPx": "88000.0",
        "triggerCondition": "sl",
        "isPositionTpsl": True,
        "tif": None,
    },
]

MISSING = object()  #: "Hyperliquid returned nothing for this wallet"

#: The same account after the position is gone. Hyperliquid drops the entry from
#: ``assetPositions`` entirely rather than reporting a zero size.
BTC_FLAT = {
    "marginSummary": {
        "accountValue": "6200000.0",
        "totalNtlPos": "0.0",
        "totalMarginUsed": "0.0",
    },
    "withdrawable": "6200000.0",
    "time": 1_700_000_100_000,
    "assetPositions": [],
}


# ── doubles ────────────────────────────────────────────────────

class PipelineHTTP(UniverseHTTP):
    """Answers the four ``/info`` requests this pipeline makes, and no more.

    ``account`` / ``orders`` may be :data:`MISSING`, which is how the real API
    behaves when a request fails or the wallet is unknown: an empty answer, not
    a fabricated one.
    """

    def __init__(self, *, account: Any = MISSING, orders: Any = MISSING) -> None:
        super().__init__()
        self.account = account
        self.orders = orders

    async def post(self, path: str, json: dict[str, Any] | None = None, **_kw: Any) -> FakeResponse:
        body = json or {}
        request_type = str(body.get("type"))
        self.types.append(request_type)
        self.calls.append({"path": path, "body": body})
        if request_type == "metaAndAssetCtxs":
            return FakeResponse(200, META_AND_CTXS)
        if request_type == "allMids":
            return FakeResponse(200, {"BTC": str(BTC_PX), "ETH": "4000.0", "SOL": "200.0"})
        if request_type == "clearinghouseState":
            return FakeResponse(200, None if self.account is MISSING else self.account)
        if request_type == "frontendOpenOrders":
            return FakeResponse(200, None if self.orders is MISSING else self.orders)
        return FakeResponse(200, [])


class PushSocket(FakeSocket):
    """A :class:`FakeSocket` a test can feed frames to after the engine starts."""

    def __init__(self) -> None:
        super().__init__([])
        self.inbox: asyncio.Queue[str] = asyncio.Queue()

    async def push(self, channel: str, data: Any) -> None:
        await self.inbox.put(json.dumps({"channel": channel, "data": data}))

    async def __anext__(self) -> str:
        return await self.inbox.get()


class Wired:
    """The started stack plus the handles a test needs to drive it."""

    def __init__(self, container: Any, socket: PushSocket, bot: FakeBot, http: PipelineHTTP) -> None:
        self.container = container
        self.socket = socket
        self.bot = bot
        self.http = http

    @property
    def engine(self) -> Any:
        return self.container.engine

    @property
    def texts(self) -> list[str]:
        return [message["text"] for message in self.bot.messages]

    async def trades(self, *frames: dict[str, Any]) -> None:
        """Push ``trades`` frames and wait until the pipeline has finished.

        No arbitrary sleeps: the engine's own work queue and the alert queue are
        joined, so "nothing was sent" is a real result rather than a race.
        """
        seen = self.engine.trades_seen + len(frames)
        for payload in frames:
            await self.socket.push("trades", [payload])
        await wait_for(lambda: self.engine.trades_seen >= seen)
        await self.engine._queue.join()
        await self.container.alerts._queue.join()

    async def user_events(self, wallet: str, payload: dict[str, Any]) -> None:
        """Deliver a ``userEvents`` frame for one focus wallet.

        The per-wallet socket is created by ``_apply_focus_slate`` on a refresh
        timer, so the handler it would install is called directly. Everything
        downstream — parser, queue, detector, filter, dedup, persistence,
        formatter — is the production path.
        """
        await self.engine._on_user_message(wallet, "userEvents", payload)
        await self.engine._queue.join()
        await self.container.alerts._queue.join()


@pytest.fixture
async def wire(container, monkeypatch):
    """Factory: start the real ingest stack against the offline seams."""
    started: list[Any] = []

    async def _wire(*, account: Any = BTC_LONG, orders: Any = BTC_TRIGGERS) -> Wired:
        socket = PushSocket()
        monkeypatch.setattr(ws_module, "ws_connect", FakeEndpoint(socket))
        http = PipelineHTTP(account=account, orders=orders)
        container.rest._client = http
        bot = FakeBot()
        container.alerts.attach_bot(bot)
        await container.alerts.start()
        await container.start_ingest()
        started.append(container)
        await wait_for(lambda: container.engine.connected)
        return Wired(container, socket, bot, http)

    yield _wire
    for app in started:
        await app.stop_ingest()


# ── the happy path, end to end ─────────────────────────────────

async def test_a_raw_trades_frame_becomes_one_formatted_whale_alert(wire):
    """$5,000,000 of BTC bought against a $2,000,000 threshold."""
    stack = await wire()
    await stack.trades(
        raw_trade(coin="BTC", px=BTC_PX, sz=50.0, side="B", users=[WALLET_A, WALLET_B], tid=77_001)
    )

    assert len(stack.bot.messages) == 1
    message = stack.bot.messages[0]
    assert message["chat_id"] == MAIN_ADMIN_ID          # the admin, no one else
    assert message["parse_mode"] == "HTML"
    text = message["text"]

    # Header, footer and the §16 divider structure.
    assert text.startswith("🐋 HYPERLIQUID WHALE ALERT")
    assert text.endswith("🐋 Whale Monitor")
    assert text.count(DIVIDER) == 3

    # Coin, direction and the value the threshold was applied to. The side is
    # the *executed* side; the position it belongs to is reported separately.
    assert "🪙 <b>BTC</b>" in text
    assert "🟢 BUY" in text
    assert "📈 LONG" not in text
    assert "💱 <b>Trade:</b> $5,000,000" in text        # executed trade value

    # Everything below came from clearinghouseState / frontendOpenOrders.
    assert "💰 <b>Position:</b> $6,000,000" in text     # not the same as the trade
    assert "🎯 <b>Entry:</b> $98,000.00" in text
    assert "⚡ <b>Leverage:</b> 5x (cross)" in text
    assert "💀 <b>Liquidation:</b> $74,500.00" in text
    assert "🎯 <b>TP:</b> $115,000.00" in text
    assert "🛑 <b>SL:</b> $88,000.00" in text
    assert "📦 <b>Size:</b> 60 BTC" in text
    assert "🏦 <b>Margin:</b> $1.18M" in text

    # Wallet formatting: the full address, copy-pasteable, no identity claim (§20).
    assert f"👤 <b>Trader:</b> <code>{WALLET_A}</code>" in text
    assert "..." not in text                             # never an abbreviation
    assert "🕐 <b>Detected:</b>" in text
    assert "🔎 <b>Detection:</b> Large market trade (executed trade value)" in text


async def test_the_alert_is_backed_by_exactly_one_persisted_event(wire, database):
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, side="B", tid=77_002))

    async with database.session() as session:
        events = await EventRepository.recent(session, limit=10)
        assert len(events) == 1
        event = events[0]
        assert event.event_type == EventType.WHALE_TRADE.value
        assert event.value_kind == ValueKind.TRADE_VALUE.value
        assert event.coin == "BTC"
        assert event.side == "BUY"                       # the executed side
        assert event.wallet == WALLET_A.lower()          # stored lower-cased
        assert event.notional == pytest.approx(5_000_000.0)
        assert event.price == pytest.approx(BTC_PX)
        assert event.position_value == pytest.approx(6_000_000.0)
        assert event.alerted is True
        assert event.dedup_key

        # The trade also refreshes the position snapshot, TP/SL included.
        position = await PositionRepository.get(session, WALLET_A, "BTC")
        assert position is not None
        assert position.take_profit_px == pytest.approx(115_000.0)
        assert position.stop_loss_px == pytest.approx(88_000.0)
        assert position.liquidation_px == pytest.approx(74_500.0)

    assert stack.engine.stats()["events_alerted"] == 1
    assert stack.engine.stats()["persist_errors"] == 0


async def test_both_participants_are_tracked_but_only_the_taker_is_alerted(wire, database):
    """The maker's side is reported through the order lifecycle, not here."""
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, side="B", users=[WALLET_A, WALLET_B], tid=77_003))

    assert len(stack.bot.messages) == 1
    assert stack.engine.tracker.get(WALLET_A) is not None
    assert stack.engine.tracker.get(WALLET_B) is not None      # observed in memory
    assert stack.engine.tracker.stats()["tracked"] >= 2
    async with database.session() as session:
        # Only the wallet an alert was raised for earns a row; the counterparty
        # is a passive observation, not a whale event.
        assert await WalletRepository.get(session, WALLET_A) is not None
        assert await WalletRepository.get(session, WALLET_B) is None


async def test_a_sell_aggressor_on_a_short_renders_as_a_sell(wire):
    """The execution is a SELL. That the wallet is short is separate information."""
    short = json.loads(json.dumps(BTC_LONG))
    short["assetPositions"][0]["position"]["szi"] = "-60.0"
    short["assetPositions"][0]["position"]["liquidationPx"] = "121000.0"
    stack = await wire(account=short, orders=[])
    await stack.trades(raw_trade(sz=50.0, side="A", users=[WALLET_B, WALLET_A], tid=77_004))

    assert len(stack.bot.messages) == 1
    text = stack.texts[0]
    assert "🔴 SELL" in text
    assert "📉 SHORT" not in text
    assert "📈 LONG" not in text
    # ``side: "A"`` means the seller was the aggressor: users[1].
    assert f"<code>{WALLET_A}</code>" in text


# ── threshold, coin filter, monitoring switch ──────────────────

async def test_a_trade_below_the_threshold_produces_nothing(wire, database):
    """5 BTC is $500,000 — below both the threshold and the discovery pre-gate."""
    stack = await wire()
    await stack.trades(raw_trade(sz=5.0, tid=77_010))

    assert stack.bot.messages == []
    assert stack.engine.trades_seen == 1
    assert stack.engine.candidates == 0
    assert stack.engine.events_detected == 0
    async with database.session() as session:
        assert await EventRepository.recent(session, limit=5) == []


async def test_a_nearly_large_trade_is_observed_but_never_alerted(wire, database):
    """$1.5M passes the discovery pre-gate, so the wallet is tracked — silently."""
    stack = await wire()
    await stack.trades(raw_trade(sz=15.0, tid=77_011))

    assert stack.bot.messages == []
    assert stack.engine.candidates == 0
    async with database.session() as session:
        assert await EventRepository.recent(session, limit=5) == []
    assert stack.engine.tracker.stats()["tracked"] >= 1


async def test_a_raised_threshold_silences_a_previously_alertable_trade(wire):
    stack = await wire()
    await stack.container.settings.set_threshold(50_000_000.0, MAIN_ADMIN_ID)
    await stack.trades(raw_trade(sz=50.0, tid=77_012))
    assert stack.bot.messages == []

    await stack.container.settings.set_threshold(2_000_000.0, MAIN_ADMIN_ID)
    await stack.trades(raw_trade(sz=50.0, tid=77_013))
    assert len(stack.bot.messages) == 1


async def test_a_coin_outside_the_filter_produces_nothing(wire):
    stack = await wire()
    await stack.container.settings.set_coins(["ETH"], MAIN_ADMIN_ID)
    await stack.trades(raw_trade(coin="BTC", sz=50.0, tid=77_014))

    assert stack.bot.messages == []
    assert stack.engine.trades_seen == 1
    assert stack.engine.candidates == 0


async def test_the_enabled_coin_still_alerts_after_the_filter_narrows(wire):
    stack = await wire()
    await stack.container.settings.set_coins(["BTC"], MAIN_ADMIN_ID)
    await stack.trades(
        raw_trade(coin="ETH", px=4_000.0, sz=1_000.0, tid=77_015),   # $4M, filtered
        raw_trade(coin="BTC", sz=50.0, tid=77_016),                  # $5M, alerted
    )

    assert len(stack.bot.messages) == 1
    assert "🪙 <b>BTC</b>" in stack.texts[0]


async def test_a_paused_monitor_produces_nothing(wire, database):
    stack = await wire()
    await stack.container.settings.set_monitoring(False, MAIN_ADMIN_ID)
    await stack.trades(raw_trade(sz=200.0, tid=77_017))              # $20M, ignored

    assert stack.bot.messages == []
    async with database.session() as session:
        assert await EventRepository.recent(session, limit=5) == []


# ── duplicate suppression ──────────────────────────────────────

async def test_the_same_trade_id_arriving_twice_alerts_once(wire, database):
    """Hyperliquid replays trades after a reconnect; ``tid`` is the identity."""
    stack = await wire()
    duplicate = raw_trade(sz=50.0, tid=77_020)
    await stack.trades(duplicate, dict(duplicate))

    assert len(stack.bot.messages) == 1
    assert stack.engine.events_detected == 2                # both reached the detector
    assert stack.engine.dedup.stats.duplicate_identity == 1
    async with database.session() as session:
        assert len(await EventRepository.recent(session, limit=5)) == 1


async def test_a_second_similar_trade_is_held_by_the_cooldown(wire):
    """Different trade, same wallet/coin/side/magnitude: noise, not news."""
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, tid=77_021), raw_trade(sz=52.0, tid=77_022))

    assert len(stack.bot.messages) == 1
    assert stack.engine.dedup.stats.cooled_down == 1


async def test_an_order_of_magnitude_larger_trade_breaks_the_cooldown(wire):
    """$5M then $60M from the same wallet: the second is a different story."""
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, tid=77_023), raw_trade(sz=600.0, tid=77_024))

    assert len(stack.bot.messages) == 2
    assert "$5,000,000" in stack.texts[0]
    assert "$60,000,000" in stack.texts[1]


async def test_a_zero_cooldown_lets_every_distinct_trade_through(wire):
    stack = await wire()
    await stack.container.settings.set_cooldown(0, MAIN_ADMIN_ID)
    await stack.trades(raw_trade(sz=50.0, tid=77_025), raw_trade(sz=51.0, tid=77_026))

    assert len(stack.bot.messages) == 2


async def test_a_redeploy_does_not_replay_the_last_alert(wire, container, database, env, monkeypatch):
    """Spec §48: the dedup cache is warmed from the database, not from RAM."""
    from app.container import AppContainer

    stack = await wire()
    replay = raw_trade(sz=50.0, tid=77_027)
    await stack.trades(replay)
    assert len(stack.bot.messages) == 1
    await container.stop_ingest()

    socket = PushSocket()
    monkeypatch.setattr(ws_module, "ws_connect", FakeEndpoint(socket))
    redeployed = AppContainer(env, database)
    await redeployed.restore()
    redeployed.rest._client = PipelineHTTP(account=BTC_LONG, orders=BTC_TRIGGERS)
    bot = FakeBot()
    redeployed.alerts.attach_bot(bot)
    await redeployed.alerts.start()
    await redeployed.start_ingest()
    try:
        await wait_for(lambda: redeployed.engine.connected)
        await Wired(redeployed, socket, bot, redeployed.rest._client).trades(dict(replay))
        assert bot.messages == []                          # the same trade, once
        assert redeployed.engine.dedup.stats.duplicate_identity == 1
    finally:
        await redeployed.stop_ingest()
        await redeployed.alerts.stop()


# ── honesty about unavailable data (spec §1 / §34) ─────────────

async def test_tp_and_sl_are_marked_unchecked_when_not_fetched(wire, database):
    """No ``frontendOpenOrders`` answer means no TP/SL claim of any kind."""
    stack = await wire(orders=MISSING)
    await stack.trades(raw_trade(sz=50.0, tid=77_030))

    text = stack.texts[0]
    assert "🎯 <b>TP:</b> N/A (not checked)" in text
    assert "🛑 <b>SL:</b> N/A (not checked)" in text
    assert "115,000" not in text
    assert "88,000" not in text
    async with database.session() as session:
        position = await PositionRepository.get(session, WALLET_A, "BTC")
        assert position is not None
        assert position.take_profit_px is None
        assert position.stop_loss_px is None


async def test_no_trigger_orders_means_no_tp_or_sl_value(wire):
    """The wallet was checked and has none: "N/A", not a guessed level."""
    stack = await wire(orders=[])
    await stack.trades(raw_trade(sz=50.0, tid=77_031))

    text = stack.texts[0]
    assert "🎯 <b>TP:</b> N/A" in text
    assert "🛑 <b>SL:</b> N/A" in text
    assert "not checked" not in text


# ── TP/SL must actually be fetched when an alert needs it ──────
#
# Feature item 3. The levels above were already rendered correctly *given* a
# trigger-order snapshot; the reported defect was that live alerts almost never
# had one, so both lines read `N/A (not checked)`. Two gates in
# ``WhaleEngine._enrich`` were the cause, and each has a test here plus one for
# the reserve that must still hold.


def leave_budget(stack, weight: float) -> None:
    """Spend the weight window down until only ``weight`` is available."""
    limiter = stack.container.rest.limiter
    spend = limiter.available - weight
    if spend > 0:
        assert limiter.try_acquire(spend)
    assert abs(limiter.available - weight) < 1.0


async def test_tpsl_is_fetched_for_an_alert_even_in_a_busy_weight_minute(wire):
    """Root cause 1: one reserve served two very different callers.

    ``frontendOpenOrders`` costs 20 of a 1200/minute budget and was only spent
    when more than 200 remained. That is the right bar for the background poll
    loop trawling every tracked wallet, and the wrong one here: this trade has
    already cleared the alert threshold, the message is about to be rendered, and
    the trader's own TP/SL is the data a whale-watcher most wants. Under the wide
    reserve a whale that arrived late in a busy minute was reported without it.
    """
    stack = await wire()
    before = stack.http.types.count("frontendOpenOrders")
    leave_budget(stack, 100.0)                       # under 200, comfortably over 20

    await stack.trades(raw_trade(sz=50.0, tid=77_050))

    assert stack.http.types.count("frontendOpenOrders") > before
    text = stack.texts[0]
    assert "🎯 <b>TP:</b> $115,000.00" in text
    assert "🛑 <b>SL:</b> $88,000.00" in text
    assert "not checked" not in text


async def test_the_reserve_still_stops_the_fetch_when_the_budget_is_nearly_gone(wire):
    """The relaxed reserve is not "no reserve".

    Below it the call is skipped and the alert says `not checked` — the honest
    answer. Spending the last of the window on a weight-20 nicety would starve
    the weight-2 ``clearinghouseState`` and ``orderStatus`` calls that the rest
    of the alert depends on.
    """
    stack = await wire()
    before = stack.http.types.count("frontendOpenOrders")
    leave_budget(stack, 30.0)                        # the call would fit; the reserve says no

    await stack.trades(raw_trade(sz=50.0, tid=77_051))

    assert stack.http.types.count("frontendOpenOrders") == before
    text = stack.texts[0]
    assert "🎯 <b>TP:</b> N/A (not checked)" in text
    assert "🛑 <b>SL:</b> N/A (not checked)" in text


async def test_tpsl_is_still_fetched_with_the_order_detector_switched_off(wire, container):
    """Root cause 2: one flag governed two responsibilities.

    ``enable_order_detector`` means "alert me about large resting orders". It was
    also the gate on the only request that reveals TP/SL, so switching off
    order *alerts* silently switched off TP/SL on every position alert too. The
    fetch is now made for an alert regardless; emission still respects the flag.
    """
    await container.settings.set_value("enable_order_detector", False, MAIN_ADMIN_ID)
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, tid=77_052))

    text = stack.texts[0]
    assert "🎯 <b>TP:</b> $115,000.00" in text
    assert "🛑 <b>SL:</b> $88,000.00" in text
    # …and no resting-order alert appeared from the detector the admin disabled.
    assert len(stack.bot.messages) == 1
    assert "LIMIT" not in text


async def test_a_missing_liquidation_price_is_reported_as_na(wire):
    no_liq = json.loads(json.dumps(BTC_LONG))
    no_liq["assetPositions"][0]["position"]["liquidationPx"] = None
    stack = await wire(account=no_liq, orders=[])
    await stack.trades(raw_trade(sz=50.0, tid=77_032))

    text = stack.texts[0]
    assert "💀 <b>Liquidation:</b> N/A" in text
    assert "💰 <b>Position:</b> $6,000,000" in text        # the rest still stands


async def test_a_wallet_with_no_position_says_so_instead_of_guessing(wire):
    """An unenriched wallet gets one honest line, not a wall of invented fields."""
    stack = await wire(account=MISSING, orders=MISSING)
    await stack.trades(raw_trade(sz=50.0, tid=77_033))

    text = stack.texts[0]
    assert "ℹ️ <b>Position data:</b> unavailable" in text
    assert "💰 <b>Position:</b>" not in text
    assert "🎯 <b>Entry:</b>" not in text
    assert "⚡ <b>Leverage:</b>" not in text
    assert "🟢 BUY" in text                                # falls back to the trade side
    assert "💱 <b>Trade:</b> $5,000,000" in text           # the trade itself is real
    assert "📦 <b>Size:</b> 50 BTC" in text                # the traded size, not a position


async def test_an_unattributed_trade_never_invents_a_wallet(wire, database):
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, users=[], tid=77_034))

    text = stack.texts[0]
    assert "👤 <b>Trader:</b> N/A" in text
    assert "<code>" not in text
    async with database.session() as session:
        events = await EventRepository.recent(session, limit=5)
        assert len(events) == 1
        assert events[0].wallet is None


# ── forced liquidations (feature item 5) ───────────────────────
#
# A liquidation reaches us only as a ``userEvents`` fill carrying a
# ``liquidation`` object — there is no public global liquidation feed
# (`.agent/API_NOTES.md` §5). These tests drive that frame through the real
# pipeline and assert on the Telegram text and the rows written.

async def test_a_liquidation_frame_becomes_a_liquidation_alert(wire):
    stack = await wire()
    # The wallet is known first, so the pre-liquidation snapshot exists.
    await stack.trades(raw_trade(sz=50.0, users=[WALLET_A, WALLET_B], tid=77_050))
    stack.bot.messages.clear()

    fill = make_liquidation_fill(px=95_000.0, sz=60.0, side="A", tid=88_001)
    await stack.user_events(WALLET_A, raw_user_events([fill]))

    assert len(stack.bot.messages) >= 1
    text = stack.texts[0]
    assert text.startswith("💥 WHALE LIQUIDATED")
    assert "🪙 <b>BTC</b>" in text
    assert "💥 <b>Liquidated value:</b> $5,700,000" in text
    assert "💵 <b>Fill price:</b> $95,000.00" in text
    assert "📦 <b>Size:</b> 60 BTC" in text
    assert "📊 <b>Mark at liquidation:</b> $95,010.00" in text
    assert f"<code>{WALLET_A.lower()}</code>" in text
    # The side came from the snapshot taken before the forced close.
    assert "📈 LONG liquidated" in text
    # Never inferred from the SELL that executed it; both appear, labelled.
    assert "🔀 <b>Fill direction:</b> SELL" in text


async def test_a_liquidation_never_reports_leverage_or_margin(wire):
    """Hyperliquid publishes neither for the moment of liquidation."""
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, users=[WALLET_A, WALLET_B], tid=77_051))
    stack.bot.messages.clear()

    await stack.user_events(
        WALLET_A, raw_user_events([make_liquidation_fill(tid=88_002)])
    )

    text = stack.texts[0]
    assert "⚡ <b>Leverage:</b>" not in text
    assert "🏦 <b>Margin:</b>" not in text
    assert "Leverage and margin at liquidation are not reported by Hyperliquid" in text


async def test_a_liquidation_writes_an_event_row_but_no_position_row(wire, database):
    """The only snapshot it holds is the pre-liquidation one; writing it would
    restore a position the exchange has just closed."""
    stack = await wire()
    await stack.user_events(
        WALLET_A, raw_user_events([make_liquidation_fill(tid=88_003)])
    )

    async with database.session() as session:
        events = await EventRepository.recent(session, limit=5)
        assert [row.event_type for row in events] == [EventType.WHALE_LIQUIDATED.value]
        assert events[0].value_kind == ValueKind.LIQUIDATION_VALUE.value
        assert events[0].wallet == WALLET_A.lower()
        assert await PositionRepository.get(session, WALLET_A, "BTC") is None


async def test_the_same_liquidation_delivered_twice_alerts_once(wire):
    stack = await wire()
    frame = raw_user_events([make_liquidation_fill(tid=88_004)])
    await stack.user_events(WALLET_A, frame)
    await stack.user_events(WALLET_A, frame)

    assert len(stack.bot.messages) == 1


async def test_a_liquidation_on_the_other_side_is_attributed_to_the_liquidated_wallet(wire):
    """A ``liquidation`` object on A's feed does not mean A was liquidated."""
    stack = await wire()
    fill = make_liquidation_fill(liquidated_user=WALLET_B, tid=88_005)
    await stack.user_events(WALLET_A, raw_user_events([fill]))

    text = stack.texts[0]
    assert f"👤 <b>Trader:</b> <code>{WALLET_B.lower()}</code>" in text
    assert f"<code>{WALLET_A.lower()}</code> (counterparty fill)" in text
    # closedPnl on that fill is A's result, not B's loss.
    assert "⚪ <b>Realized PnL:</b> N/A" in text


async def test_the_forced_refetch_still_produces_the_real_position_close(wire, database):
    """Two facts, two alerts: the exchange forced a close, and the book is flat."""
    stack = await wire()
    await stack.trades(raw_trade(sz=50.0, users=[WALLET_A, WALLET_B], tid=77_052))
    stack.bot.messages.clear()
    stack.http.account = BTC_FLAT           # the position is gone from now on

    await stack.user_events(
        WALLET_A, raw_user_events([make_liquidation_fill(tid=88_006)])
    )

    headers = [text.splitlines()[0] for text in stack.texts]
    assert headers == ["💥 WHALE LIQUIDATED", "🐋 WHALE POSITION CLOSED"]
    async with database.session() as session:
        position = await PositionRepository.get(session, WALLET_A, "BTC")
        assert position is not None and position.is_open is False


async def test_a_liquidation_below_the_position_threshold_is_not_alerted(wire):
    stack = await wire()
    await stack.user_events(
        WALLET_A, raw_user_events([make_liquidation_fill(px=95_000.0, sz=1.0, tid=88_007)])
    )

    assert stack.bot.messages == []


async def test_a_plain_fill_without_a_liquidation_object_is_not_a_liquidation(wire):
    from tests.factories import make_fill

    stack = await wire()
    await stack.user_events(WALLET_A, raw_user_events([make_fill(tid=88_008)]))

    assert [text for text in stack.texts if "LIQUIDATED" in text] == []


# ── malformed input must not reach an alert ────────────────────

async def test_a_malformed_trade_frame_is_dropped_quietly(wire):
    stack = await wire()
    await stack.socket.push("trades", [{"coin": "BTC", "px": "not-a-number", "sz": "50"}])
    await stack.socket.push("trades", "this is not a list")
    await stack.socket.push("trades", [{"sz": "50", "px": "100000"}])       # no coin
    await stack.trades(raw_trade(sz=50.0, tid=77_040))                      # a real one

    assert len(stack.bot.messages) == 1
    assert stack.engine.trades_seen == 1                                    # only the real trade
    assert stack.engine.connected is True


async def test_an_unknown_coin_is_not_alerted(wire):
    """A perp the configured filter does not list must not slip through."""
    stack = await wire()
    await stack.trades(raw_trade(coin="DOGE", px=0.4, sz=50_000_000.0, tid=77_041))

    assert stack.bot.messages == []
    assert stack.engine.candidates == 0
