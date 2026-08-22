"""Admin UI, wallet display and data-integrity regressions.

Task "CLEXER — ADMIN UI + WALLET DISPLAY + DATA INTEGRITY AUDIT/FIX" §15: the
named regression tests for the fourteen reported defects. Each one is pinned to
the *root cause* rather than to the Telegram text where the symptom appeared,
because the task is explicit that changing the wording is not a fix:

    "DO NOT only change the Telegram text. Inspect the underlying
    implementation, identify the root causes, fix them, add regression tests,
    and verify the complete system."

What each group locks down:

* **1/2/3 — wallet display.** ``0x3200...c407`` was unusable: it cannot be
  pasted into a block explorer and two whales can share a prefix and a suffix.
  Every list view, every alert and every admin panel now renders the complete
  42-character address in monospace, and the stored value was never truncated.
* **3/13 — the co-admin list button.** It re-rendered the panel it sat on, so
  Telegram rejected the edit as "message is not modified" and ``respond``
  swallowed that — a dead button with no error. It now has its own view.
* **4/5/14 — command scopes.** Visibility is published per Telegram scope, and
  authority is still re-derived server-side from ``effective_user.id``.
* **6 — duplicate events.** One ETH buy announced twice a second apart, because
  the identity key mixed in ``event.side``, which enrichment rewrites from
  ``BUY`` to ``LONG``. Keys are now built from feed-derived facts only.
* **7 — threshold semantics.** A $4.60M order under a "$5,000,000" heading.
* **8 — "Alerts delivered: 0".** A legitimate state (nobody subscribed) that
  looked identical to a fault because it was only logged at debug level.
* **9/10/11 — never invent data.** No owner for an aggregated ``l2Book`` level,
  no notional for a position with no snapshot behind it, no liquidation price
  Hyperliquid did not publish.
* **12 — callback payloads.** No ``callback_data`` carries a wallet; the
  database keeps the complete address and the view resolves it from there.
"""

from __future__ import annotations

import re

import pytest
from telegram import BotCommandScopeChat, BotCommandScopeDefault

from app.bot.commands import (
    ADMIN_COMMAND_NAMES,
    CO_ADMIN_COMMAND_MENU,
    MAIN_ADMIN_COMMAND_MENU,
    MAIN_ADMIN_COMMAND_NAMES,
    PUBLIC_COMMAND_MENU,
    publish_command_menus,
)
from app.bot.handlers import COMMANDS
from app.bot.handlers import admin as admin_cmds
from app.bot.handlers import common, data
from app.bot.handlers.callbacks import on_callback
from app.bot.keyboards import inline
from app.bot.messages import texts
from app.database.repository import (
    EventRepository,
    OrderRepository,
    PositionRepository,
    UserRepository,
)
from app.services.admin_service import ROLE_CO
from app.services.settings_service import RuntimeConfig
from app.utils.formatting import Confidence, DataPoint, short_wallet
from app.whale.dedup import Deduplicator, identity_key
from app.whale.detector import WhaleDetector
from app.whale.events import EventType, ValueKind, WhaleEvent
from app.whale.filters import REASON_OK, REASON_THRESHOLD, WhaleFilter
from tests.conftest import (
    CO_ADMIN_ID,
    MAIN_ADMIN_ID,
    STRANGER_ID,
    FakeBot,
    FakeUpdate,
)
from tests.factories import (
    WALLET_A,
    WALLET_B,
    WALLET_C,
    ago,
    make_book,
    make_position,
    now,
    price_map,
)

#: The shape of the reported defect: a leading fragment, an ellipsis, a tail.
TRUNCATED = re.compile(r"0x[0-9a-fA-F]{2,10}(\.{2,3}|…)")


# ── helpers ────────────────────────────────────────────────────

async def promote(container, telegram_id: int = CO_ADMIN_ID) -> None:
    await container.admins.add_co_admin(MAIN_ADMIN_ID, telegram_id, username="helper")


async def seed_rows(database) -> None:
    """One whale event, one resting order and one position, all with wallets."""
    async with database.session() as session:
        await EventRepository.insert(
            session,
            event_type=EventType.WHALE_TRADE.value,
            coin="BTC",
            side="LONG",
            wallet=WALLET_A.lower(),
            notional=8_130_000.0,
            value_kind=ValueKind.TRADE_VALUE.value,
            price=100_000.0,
            size=81.3,
            entry_px=98_000.0,
            leverage=5.0,
            detail={},
            dedup_key="seed-trade",
            event_time=ago(30),
        )
        await OrderRepository.upsert(
            session,
            WALLET_B,
            9_911_001,
            coin="ETH",
            side="SELL",
            limit_px=4_100.0,
            size=1_200.0,
            notional=4_920_000.0,
            status="open",
            placed_at=ago(120),
        )
        await PositionRepository.upsert(
            session,
            WALLET_C,
            "SOL",
            side="SHORT",
            size=-40_000.0,
            entry_px=205.0,
            position_value=8_200_000.0,
            liquidation_px=260.0,
            leverage=3.0,
        )


def trade_event(
    *,
    tid: int | None = 8_130_001,
    side: str = "BUY",
    notional: float = 8_130_000.0,
    wallet: str | None = WALLET_A,
) -> WhaleEvent:
    """The reported ETH buy. ``side`` is what the *feed* said on this pass."""
    event = WhaleEvent(
        event_type=EventType.WHALE_TRADE,
        coin="ETH",
        notional=notional,
        value_kind=ValueKind.TRADE_VALUE,
        side=side,
        wallet=wallet,
        detection="Large market trade",
        context={"tid": tid, "role": "taker"},
    )
    event.set("trade_side", DataPoint.confirmed("BUY"))
    event.set("price", DataPoint.confirmed(4_000.0))
    event.set("size", DataPoint.confirmed(notional / 4_000.0))
    event.set("position_value", DataPoint.confirmed(notional))
    return event


def order_event(*, oid: int, notional: float = 4_600_000.0) -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.ORDER_PLACED,
        coin="ETH",
        notional=notional,
        value_kind=ValueKind.ORDER_NOTIONAL,
        side="SELL",
        wallet=WALLET_A,
        detection="Large limit order placed",
        order_id=oid,
        status="open",
    )
    event.set("price", DataPoint.confirmed(4_100.0))
    event.set("size", DataPoint.confirmed(notional / 4_100.0))
    return event


def position_event(*, side: str, position_value: float | None = 6_000_000.0) -> WhaleEvent:
    """A position event whose side may be an *execution* side — issue 10."""
    event = WhaleEvent(
        event_type=EventType.POSITION_OPENED,
        coin="BTC",
        notional=position_value or 0.0,
        value_kind=ValueKind.POSITION_DELTA,
        side=side,
        wallet=WALLET_A,
        detection="Position opened",
        context={},
    )
    event.set("size", DataPoint.confirmed(60.0))
    if position_value is not None:
        event.set("position_value", DataPoint.confirmed(position_value))
    event.dedup_key = f"pos-{side}"
    return event


def make_filter(config: RuntimeConfig) -> WhaleFilter:
    class _Stub:
        def __init__(self, cfg: RuntimeConfig) -> None:
            self.config = cfg

    return WhaleFilter(_Stub(config))


# ── issues 1, 2, 3: the wallet is shown in full, in monospace ──

async def test_every_list_view_shows_the_complete_wallet_address(container, ctx, database):
    """Issue 1: ``/whales``, ``/orders``, ``/positions`` and ``/wallets``."""
    await seed_rows(database)
    await container.settings.add_tracked_wallet(WALLET_A, MAIN_ADMIN_ID)

    for handler, wallet in (
        (data.cmd_whales, WALLET_A),
        (data.cmd_orders, WALLET_B),
        (data.cmd_positions, WALLET_C),
        (data.cmd_wallets, WALLET_A),
    ):
        update = FakeUpdate(MAIN_ADMIN_ID, text="/x")
        await handler(update, ctx)
        text = update.last
        assert wallet.lower() in text, f"{handler.__name__} dropped the address"
        assert f"<code>{wallet.lower()}</code>" in text, f"{handler.__name__} is not monospace"


async def test_the_alert_body_shows_the_complete_wallet_address(container):
    """Issue 1: the alert itself, not only the list views."""
    rendered = container.alerts.render(trade_event())
    assert "👤 <b>Trader:</b>" in rendered
    # On its own line, complete, in monospace (spec §4/§21).
    assert f"\n<code>{WALLET_A.lower()}</code>" in rendered


async def test_the_wallet_is_monospace_and_never_bold_instead(container, ctx, database):
    """Issue 2: backticks, so Telegram offers tap-to-copy. Bold is not a substitute."""
    await seed_rows(database)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/whales")
    await data.cmd_whales(update, ctx)
    assert f"<code>{WALLET_A.lower()}</code>" in update.last
    assert f"<b>{WALLET_A.lower()}</b>" not in update.last


async def test_no_view_or_alert_truncates_a_wallet_address(container, ctx, database):
    """Issue 1: ``0x3200...c407`` must appear nowhere a human reads."""
    await seed_rows(database)
    await container.settings.add_tracked_wallet(WALLET_A, MAIN_ADMIN_ID)

    rendered = [container.alerts.render(trade_event())]
    for handler in (data.cmd_whales, data.cmd_orders, data.cmd_positions, data.cmd_wallets):
        update = FakeUpdate(MAIN_ADMIN_ID, text="/x")
        await handler(update, ctx)
        rendered.append(update.last)

    for text in rendered:
        assert TRUNCATED.search(text) is None, f"an abbreviated address survives: {text[:120]}"
        assert short_wallet(WALLET_A) not in text


def test_the_stored_value_is_the_complete_address():
    """Issue 1: ``short_wallet`` is a button label, never the canonical value."""
    assert short_wallet(WALLET_A) != WALLET_A
    assert len(WALLET_A) == 42
    # The one canonical renderer keeps all 42 characters.
    from app.utils.formatting import wallet_code

    assert WALLET_A.lower() in wallet_code(WALLET_A)


async def test_the_complete_wallet_survives_database_persistence(container, database):
    """Issue 1/12: the DB column is 42 characters and nothing shortens it."""
    await seed_rows(database)
    async with database.session() as session:
        event = (await EventRepository.recent(session, limit=1))[0]
        order = (await OrderRepository.open_orders(session, limit=1))[0]
        position = (await PositionRepository.open_positions(session, limit=1))[0]
        # Round-tripped by the exact address, which only works if it was stored whole.
        looked_up = await PositionRepository.get(session, WALLET_C, "SOL")

    assert event.wallet == WALLET_A.lower()
    assert order.wallet == WALLET_B.lower()
    assert position.wallet == WALLET_C.lower()
    assert looked_up is not None and looked_up.wallet == WALLET_C.lower()
    assert all(len(row) == 42 for row in (event.wallet, order.wallet, position.wallet))


# ── issue 3: the Co-Admin List button ──────────────────────────

async def test_the_co_admin_list_button_renders_its_own_panel(container, ctx):
    """Issue 3: ``adm:list`` produced identical text to ``adm:open``, so Telegram
    refused the edit and ``respond`` swallowed the "not modified" error — the
    button looked dead. The two views must differ."""
    await promote(container)

    home = FakeUpdate(MAIN_ADMIN_ID, callback_data="adm:open")
    await on_callback(home, ctx)
    roster = FakeUpdate(MAIN_ADMIN_ID, callback_data="adm:list")
    await on_callback(roster, ctx)

    assert home.callback_query.edits, "the admin home did not render"
    assert roster.callback_query.edits, "the list button rendered nothing"
    assert roster.callback_query.edits[0] != home.callback_query.edits[0]
    assert "CO-ADMIN ROSTER" in roster.callback_query.edits[0]
    assert f"<code>{CO_ADMIN_ID}</code>" in roster.callback_query.edits[0]
    # The press is acknowledged, so the client stops spinning even on a no-op.
    assert roster.callback_query.answers


async def test_a_co_admin_may_open_the_roster_but_a_stranger_may_not(container, ctx):
    """Issue 3: reading the roster is VIEW_ADMINS; an ordinary user has neither."""
    await promote(container)

    allowed = FakeUpdate(CO_ADMIN_ID, callback_data="adm:list")
    await on_callback(allowed, ctx)
    assert allowed.callback_query.edits
    assert allowed.callback_query.alerts == []

    refused = FakeUpdate(STRANGER_ID, callback_data="adm:list")
    await on_callback(refused, ctx)
    assert refused.callback_query.edits == []
    assert refused.callback_query.alerts
    assert str(CO_ADMIN_ID) not in refused.callback_query.alerts[0]


# ── issues 4, 5, 13, 14: visibility and authority ──────────────

async def test_a_normal_user_cannot_execute_an_admin_command(container, ctx):
    """Issue 4: hiding a command is not security. Typing it must still be refused."""
    before = container.settings.config.min_whale_value
    update = FakeUpdate(STRANGER_ID, text="/setthreshold 1")
    ctx.args = ["1"]
    await admin_cmds.cmd_setthreshold(update, ctx)

    assert container.settings.config.min_whale_value == before
    assert "currently private" in update.last

    paused = FakeUpdate(STRANGER_ID, text="/pause")
    await admin_cmds.cmd_pause(paused, ctx)
    assert container.settings.config.paused is False
    assert "currently private" in paused.last


async def test_a_co_admin_cannot_execute_a_main_admin_only_command(container, ctx):
    """Issue 13: MAIN_ONLY capabilities are refused through the command route too."""
    await promote(container)
    ctx.args = [str(STRANGER_ID)]
    update = FakeUpdate(CO_ADMIN_ID, text=f"/addadmin {STRANGER_ID}")
    await admin_cmds.cmd_addadmin(update, ctx)

    assert container.admins.role_of(STRANGER_ID) != ROLE_CO
    assert "Only the Main Admin" in update.last


async def test_the_default_command_menu_excludes_every_admin_command(container):
    """Issues 4/5/14: the ✚ menu an ordinary user sees is the default scope."""
    await promote(container)
    bot = FakeBot()
    published = await publish_command_menus(bot, container)

    assert published["default"] == 1
    default = next(
        entry for entry in bot.menus if isinstance(entry["scope"], BotCommandScopeDefault)
    )
    names = {command.command for command in default["commands"]}
    assert not names & ADMIN_COMMAND_NAMES
    assert not names & MAIN_ADMIN_COMMAND_NAMES
    assert {"start", "stop", "help", "whales"} <= names


async def test_each_admin_gets_the_menu_their_role_actually_carries(container):
    """Issue 4/14: verified on the request sent to Telegram, not on a local registry."""
    await promote(container)
    bot = FakeBot()
    await publish_command_menus(bot, container)

    by_chat = {
        entry["scope"].chat_id: entry["commands"]
        for entry in bot.menus
        if isinstance(entry["scope"], BotCommandScopeChat)
    }
    assert by_chat[MAIN_ADMIN_ID] == MAIN_ADMIN_COMMAND_MENU
    assert by_chat[CO_ADMIN_ID] == CO_ADMIN_COMMAND_MENU
    co_admin_names = {command.command for command in by_chat[CO_ADMIN_ID]}
    assert not co_admin_names & MAIN_ADMIN_COMMAND_NAMES   # no /addadmin, /audit
    assert {"pause", "go", "panel"} <= co_admin_names


async def test_a_demoted_co_admin_loses_their_command_scope(container, ctx):
    """Issue 14: Telegram keeps a chat scope until it is deleted."""
    await promote(container)
    ctx.args = [str(CO_ADMIN_ID)]
    update = FakeUpdate(MAIN_ADMIN_ID, text=f"/removeadmin {CO_ADMIN_ID}")
    await admin_cmds.cmd_removeadmin(update, ctx)

    deleted = [scope.chat_id for scope in ctx.bot.deleted_menus]
    assert CO_ADMIN_ID in deleted
    republished = [
        entry["scope"].chat_id
        for entry in ctx.bot.menus
        if isinstance(entry["scope"], BotCommandScopeChat)
    ]
    assert CO_ADMIN_ID not in republished


async def test_a_normal_user_is_never_shown_an_admin_identity_or_control(container, ctx):
    """Issue 5: no admin id, no roster, no privileged command, anywhere."""
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    await promote(container)

    seen: list[str] = []
    for handler in (common.cmd_start, common.cmd_help, data.cmd_status, data.cmd_whales):
        update = FakeUpdate(STRANGER_ID, text="/x")
        await handler(update, ctx)
        seen.append(update.last)

    for text in seen:
        assert str(MAIN_ADMIN_ID) not in text
        assert str(CO_ADMIN_ID) not in text
        leaked = [name for name in ADMIN_COMMAND_NAMES if re.search(rf"/{name}\b", text)]
        assert leaked == [], f"admin commands disclosed to a normal user: {leaked}"


async def test_what_is_advertised_publicly_is_exactly_what_a_user_may_invoke(
    container, ctx
):
    """Issues 4/5, root cause: three sources of truth must agree.

    The command menu (``commands.py``), the help text (``texts.help_text``) and
    the capability on the handler are written by hand in three places, and they
    drifted: ``/recent`` was published in the admin scope while its handler
    required only ``VIEW_WHALES`` and the public help advertised it. Rather than
    re-assert the lists, this drives every command as an ordinary user and checks
    that visibility matches authority in both directions.

    Each command gets its own caller id: the rate limiter is per user, and
    thirty-odd invocations from one id would be throttled rather than answered.
    """
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    by_name = dict(COMMANDS)
    refusal = "not authorized to use this control"

    public_names = [command.command for command in PUBLIC_COMMAND_MENU]
    assert set(public_names) <= set(by_name), "the public menu advertises an unbound command"

    for index, name in enumerate(public_names):
        update = FakeUpdate(STRANGER_ID + 100 + index, text=f"/{name}")
        await by_name[name](update, ctx)
        assert refusal not in update.last, f"/{name} is advertised publicly but refused"

    for index, name in enumerate(sorted(ADMIN_COMMAND_NAMES)):
        update = FakeUpdate(STRANGER_ID + 200 + index, text=f"/{name}")
        await by_name[name](update, ctx)
        assert refusal in update.last, f"/{name} is admin-only but a user could invoke it"


# ── issue 6: event identity ────────────────────────────────────

def test_one_fill_observed_twice_is_alerted_once():
    """Issue 6: the reported ETH BUY $8.13M, announced twice one second apart.

    The second observation arrived after enrichment had rewritten ``side`` from
    the trade side to the position side, and the old identity key included that
    field — so the same ``tid`` produced two keys. Only feed-derived facts may
    appear in the key.
    """
    first = trade_event(side="BUY")
    second = trade_event(side="LONG")        # same tid, enriched a second later
    second.event_time = first.event_time

    assert identity_key(first) == identity_key(second)
    dedup = Deduplicator()
    assert dedup.check(first, cooldown_seconds=0) is True
    assert dedup.check(second, cooldown_seconds=0) is False
    assert dedup.stats.duplicate_identity == 1


def test_the_same_wallet_with_different_order_ids_stays_separate():
    """Issue 6: deduplication must not swallow two genuinely different orders."""
    dedup = Deduplicator()
    assert dedup.check(order_event(oid=1), cooldown_seconds=0) is True
    assert dedup.check(order_event(oid=2), cooldown_seconds=0) is True
    assert identity_key(order_event(oid=1)) != identity_key(order_event(oid=2))


def test_two_different_fills_are_not_merged():
    """The converse of the fix: distinct tids stay distinct."""
    assert identity_key(trade_event(tid=1)) != identity_key(trade_event(tid=2))


async def test_a_duplicate_that_outlived_the_memory_cache_is_still_suppressed(
    container, database
):
    """Issue 6: a redeploy replays websocket snapshots, and the TTL cache is RAM.

    ``whale_events.dedup_key`` is the durable record of what was already
    recorded, so it is consulted before writing.
    """
    event = trade_event()
    Deduplicator().check(event, cooldown_seconds=0)
    async with database.session() as session:
        await EventRepository.insert(
            session,
            event_type=EventType.WHALE_TRADE.value,
            coin="ETH",
            side="LONG",
            wallet=WALLET_A.lower(),
            notional=8_130_000.0,
            value_kind=ValueKind.TRADE_VALUE.value,
            detail={},
            dedup_key=event.dedup_key,
            event_time=now(),
        )

    assert await container.engine._already_recorded(event) is True
    fresh = trade_event(tid=999_999)
    Deduplicator().check(fresh, cooldown_seconds=0)
    assert await container.engine._already_recorded(fresh) is False


# ── issue 7: threshold semantics ───────────────────────────────

def test_a_4_60m_order_is_excluded_at_a_5m_threshold():
    """Issue 7: the reported "$5,000,000" heading over a $4.60M row."""
    config = RuntimeConfig(
        all_coins=True, min_whale_value=5_000_000.0, enable_order_alerts=True
    )
    result = make_filter(config).evaluate(order_event(oid=1, notional=4_600_000.0), config)
    assert result.accepted is False
    assert result.reason == REASON_THRESHOLD
    assert result.threshold == 5_000_000.0


def test_exactly_5m_passes_because_the_configured_gate_is_inclusive():
    """Issue 7: the boundary follows the implemented ``>=``, and it is stated."""
    config = RuntimeConfig(
        all_coins=True, min_whale_value=5_000_000.0, enable_order_alerts=True
    )
    result = make_filter(config).evaluate(order_event(oid=1, notional=5_000_000.0), config)
    assert result.accepted is True
    assert result.reason == REASON_OK

    footer = "\n".join(texts._threshold_footer(config, "order"))
    assert "≥" in footer
    assert "$5,000,000" in footer
    assert "historical" in footer


def test_the_order_view_states_the_per_class_threshold_it_applied():
    """Issue 7: one "Threshold:" line over per-class gates was the ambiguity."""
    config = RuntimeConfig(all_coins=True, min_whale_value=5_000_000.0, min_order_value=2_000_000.0)
    rendered = texts.order_list(
        [
            type(
                "Row",
                (),
                {
                    "coin": "ETH",
                    "side": "SELL",
                    "notional": 4_600_000.0,
                    "limit_px": 4_100.0,
                    "status": "open",
                    "placed_at": ago(60),
                    "wallet": WALLET_A.lower(),
                },
            )()
        ],
        config,
    )
    assert "resting orders $2,000,000" in rendered
    assert "an order is not a position" in rendered


# ── issue 8: the alert pipeline ────────────────────────────────

async def test_the_alert_pipeline_delivers_when_the_configuration_allows_it(container):
    """Issue 8: 411 whale events and "Alerts delivered: 0" — prove the path works."""
    bot = FakeBot()
    container.alerts.attach_bot(bot)
    await container.alerts.start()

    event = trade_event()
    event.dedup_key = "pipeline-ok"
    await container.alerts.enqueue(event)
    await container.alerts._queue.join()

    assert [message["chat_id"] for message in bot.messages] == [MAIN_ADMIN_ID]
    assert container.alerts.stats()["sent"] == 1
    assert container.alerts.stats()["no_recipients"] == 0
    assert container.runtime_warnings() == []


async def test_the_pipeline_never_drops_an_alert_silently(container, database):
    """Issue 8: "no recipient" is legitimate, but it must be counted and said.

    The reported production state — every admin unsubscribed — was only logged at
    debug level, so it was indistinguishable from a broken pipeline.
    """
    async with database.session() as session:
        await UserRepository.upsert(session, MAIN_ADMIN_ID, MAIN_ADMIN_ID)
        await UserRepository.set_subscribed(session, MAIN_ADMIN_ID, False)
    container.alerts.invalidate_recipients()

    bot = FakeBot()
    container.alerts.attach_bot(bot)
    await container.alerts.start()
    event = trade_event()
    event.dedup_key = "pipeline-nobody"
    await container.alerts.enqueue(event)
    await container.alerts._queue.join()

    stats = container.alerts.stats()
    assert bot.messages == []
    assert stats["no_recipients"] == 1
    assert stats["dropped"] == 1
    warnings = container.runtime_warnings()
    assert any("no recipient" in warning for warning in warnings)
    panel = texts.status_panel(container.settings.config, {"alerts": stats})
    assert "Alerts with no recipient" in panel
    assert "/start to re-subscribe" in panel


# ── issues 9, 10, 11: never invent data ────────────────────────

def test_an_aggregated_book_level_never_claims_an_owner():
    """Issue 9: ``l2Book`` publishes px/sz/n only. "Trader: N/A" is correct."""
    detector = WhaleDetector(lambda coin: price_map().get(coin))
    events = detector.from_book(
        make_book(bids=[(99_000.0, 60.0)]), min_notional=1_000_000.0
    )
    assert len(events) == 1
    event = events[0]
    assert event.wallet is None
    assert event.point("wallet_attribution").confidence is Confidence.UNAVAILABLE
    assert "aggregated" in (event.point("wallet_attribution").note or "")


async def test_the_book_alert_says_why_the_owner_is_unavailable(container):
    detector = WhaleDetector(lambda coin: price_map().get(coin))
    event = detector.from_book(make_book(asks=[(101_000.0, 60.0)]), min_notional=1_000_000.0)[0]
    rendered = container.alerts.render(event)
    assert "👤 <b>Trader:</b> N/A —" in rendered
    assert "0x" not in rendered


async def test_a_position_row_without_a_snapshot_says_so_instead_of_a_number(
    container, ctx, database
):
    """Issue 10: the reported ``BTC BUY N/A``. No notional is invented, and the
    row explains itself rather than being dressed up."""
    async with database.session() as session:
        await PositionRepository.upsert(
            session, WALLET_A, "BTC", side="BUY", size=1.0, position_value=None
        )
    update = FakeUpdate(MAIN_ADMIN_ID, text="/positions")
    await data.cmd_positions(update, ctx)

    assert "N/A" in update.last
    assert "no confirmed clearinghouseState snapshot" in update.last


async def test_an_execution_side_can_never_be_written_as_a_position(container, database):
    """Issue 10, root cause: a ``BUY`` reached the positions table with no
    ``clearinghouseState`` snapshot behind it, which is what produced the
    ``BTC BUY — Notional: N/A`` rows. Only LONG/SHORT may be written."""
    engine = container.engine
    event_id = await engine._persist(position_event(side="BUY"))

    assert event_id is not None                       # the event is still recorded
    async with database.session() as session:
        assert await PositionRepository.get(session, WALLET_A, "BTC") is None
    assert engine.stats()["position_writes_skipped"] == 1

    verified = position_event(side="LONG")
    verified.set("position_side", DataPoint.confirmed("LONG"))
    assert await engine._persist(verified) is not None
    async with database.session() as session:
        row = await PositionRepository.get(session, WALLET_A, "BTC")
    assert row is not None and row.side == "LONG"


def test_no_liquidation_price_is_invented():
    """Issue 11: Hyperliquid does not always publish ``liquidationPx``."""
    detector = WhaleDetector(lambda coin: price_map().get(coin))
    event = detector.from_position_change(
        WALLET_A, "BTC", None, make_position(liquidation_px=None), min_notional=0
    )
    assert event is not None
    point = event.point("liquidation_px")
    assert point.value is None
    assert point.confidence is Confidence.UNAVAILABLE
    assert point.note

    with_price = detector.from_position_change(
        WALLET_A, "ETH", None, make_position(coin="ETH", liquidation_px=74_500.0), min_notional=0
    )
    assert with_price is not None
    assert with_price.point("liquidation_px").value == pytest.approx(74_500.0)
    assert with_price.point("liquidation_px").confidence is Confidence.CONFIRMED


async def test_the_alert_prints_liquidation_na_rather_than_a_computed_guess(container):
    detector = WhaleDetector(lambda coin: price_map().get(coin))
    event = detector.from_position_change(
        WALLET_A, "BTC", None, make_position(liquidation_px=None), min_notional=0
    )
    rendered = container.alerts.render(event)
    assert "💀 <b>Liquidation:</b> N/A" in rendered


# ── issue 12: callback payloads ────────────────────────────────

def _every_callback_payload() -> list[str]:
    config = RuntimeConfig(all_coins=False, coins=["BTC", "ETH"])
    markups = [
        inline.control_panel(config),
        inline.control_panel(RuntimeConfig(paused=True)),
        inline.monitoring_controls(config),
        inline.monitoring_controls(RuntimeConfig(paused=True)),
        inline.threshold_panel(config),
        inline.margin_panel(config),
        inline.cooldown_panel(config),
        inline.coin_panel(config, ["BTC", "ETH", "SOL"]),
        inline.admin_panel(),
        inline.admin_roster_panel(),
        inline.admin_remove_panel([{"telegram_id": CO_ADMIN_ID, "username": "helper"}]),
        inline.public_mode_panel(config),
        inline.settings_panel(config),
        inline.alert_settings_panel(config),
        inline.public_menu(),
        inline.data_panel("whales", admin=True),
        inline.data_panel("whales", admin=False),
        inline.stats_panel(admin=True),
        inline.cancel_prompt(),
    ]
    payloads: list[str] = []
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data is not None:
                    payloads.append(button.callback_data)
    return payloads


def test_no_callback_payload_carries_a_wallet_address():
    """Issue 12: a 42-character address in ``callback_data`` risks Telegram's
    64-byte limit. Buttons carry short internal ids; the wallet is resolved from
    the database, which keeps the complete address."""
    payloads = _every_callback_payload()
    assert payloads
    for payload in payloads:
        assert not re.search(r"0x[0-9a-fA-F]{6,}", payload), payload
        assert len(payload.encode("utf-8")) <= 64, payload


async def test_a_wallet_is_resolved_from_the_database_not_from_the_payload(
    container, ctx, database
):
    """Issue 12: the view reads the complete address out of storage."""
    await seed_rows(database)
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="data:positions")
    await on_callback(update, ctx)

    assert update.callback_query.edits
    rendered = update.callback_query.edits[0]
    assert f"<code>{WALLET_C.lower()}</code>" in rendered
    assert WALLET_C.lower() not in (update.callback_query.data or "")
