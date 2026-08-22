"""Persistence.

Spec §36: "Database operations". Spec §48/§49: nothing critical lives only in
RAM — every administrator decision must survive a Railway restart or redeploy.

These tests run against a real SQLite engine with the real schema and the real
repository code. Nothing is mocked: an "upsert" is asserted by writing twice and
reading back, and a "restart" is asserted by constructing a brand new service or
container over the same database, exactly as a redeploy does.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.container import AppContainer
from app.database import repository as repo_module
from app.database.base import Base, Database, get_database
from app.database.models import BotLog, OrderRecord, Setting
from app.database.repository import (
    AdminRepository,
    AlertRepository,
    AuditRepository,
    CoinRepository,
    EventRepository,
    LogRepository,
    OrderRepository,
    PositionRepository,
    SettingsRepository,
    UserRepository,
    WalletRepository,
)
from app.services.admin_service import ROLE_CO, ROLE_MAIN, AdminService, Capability
from app.services.settings_service import (
    KEY_MIN_WHALE,
    KEY_MONITORING,
    KEY_PUBLIC_MODE,
    SettingsService,
)
from app.utils.formatting import utc_now
from app.whale.events import EXECUTION_TYPE_NAMES, summarize_events
from tests.conftest import (
    CO_ADMIN_ID,
    MAIN_ADMIN_ID,
    STRANGER_ID,
    TEST_DB_URL,
    make_settings,
)
from tests.factories import WALLET_A, WALLET_B, WALLET_C, ago

# The repository module is the single data-access layer; these two numbers are
# asserted so the .agent documentation can never drift from the code again.
EXPECTED_TABLES = 12
EXPECTED_REPOSITORIES = 11


# ── schema ─────────────────────────────────────────────────────

async def test_the_schema_has_twelve_tables(database: Database):
    async with database.engine.connect() as conn:
        names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    assert len(names) == EXPECTED_TABLES
    assert set(names) == set(Base.metadata.tables)


def test_there_are_eleven_repository_classes():
    classes = {
        name
        for name, obj in vars(repo_module).items()
        if isinstance(obj, type) and name.endswith("Repository")
    }
    assert len(classes) == EXPECTED_REPOSITORIES
    assert "SettingsRepository" in classes and "LogRepository" in classes


async def test_get_database_returns_the_installed_instance(database: Database):
    assert get_database() is database


def test_the_engine_is_unavailable_before_connect():
    db = Database(TEST_DB_URL)
    with pytest.raises(RuntimeError, match="connect"):
        _ = db.engine


async def test_healthcheck_reports_a_live_connection(database: Database):
    assert await database.healthcheck() is True
    stats = database.stats()
    assert stats["connected"] is True
    assert stats["last_error"] is None
    assert "sqlite" in stats["dialect"]


async def test_healthcheck_reports_an_unreachable_database():
    """``/health`` must say ``unhealthy`` instead of raising (spec §49)."""
    db = Database("sqlite+aiosqlite:///./no_such_directory/whales.db")
    await db.connect()
    try:
        assert await db.healthcheck() is False
        stats = db.stats()
        assert stats["connected"] is False
        assert stats["last_error"]                  # a reason, not a traceback
        assert stats["last_ok_at"] is None
    finally:
        await db.disconnect()


async def test_a_disconnected_pool_is_rebuilt_on_demand():
    """``session()`` reconnects lazily, which is how a Postgres restart heals."""
    db = Database(TEST_DB_URL)
    await db.connect()
    await db.disconnect()
    try:
        assert await db.healthcheck() is True
        assert db.stats()["connected"] is True
    finally:
        await db.disconnect()


# ── transactions ───────────────────────────────────────────────

async def test_a_session_commits_on_success(database: Database):
    async with database.session() as session:
        await SettingsRepository.set(session, "probe", "committed")
    async with database.session() as session:
        assert await SettingsRepository.get(session, "probe") == "committed"


async def test_a_failing_session_rolls_back_and_re_raises(database: Database):
    with pytest.raises(RuntimeError, match="boom"):
        async with database.session() as session:
            await SettingsRepository.set(session, "ghost", "should-not-persist")
            raise RuntimeError("boom")
    async with database.session() as session:
        assert await SettingsRepository.get(session, "ghost") is None


async def test_a_rollback_discards_every_write_in_the_unit_of_work(database: Database):
    """Partial persistence would leave the bot in a state no admin chose."""
    with pytest.raises(ValueError):
        async with database.session() as session:
            await SettingsRepository.set(session, "first", "1")
            await CoinRepository.add(session, "DOGE")
            await UserRepository.upsert(session, telegram_id=STRANGER_ID, chat_id=STRANGER_ID)
            raise ValueError("half way")
    async with database.session() as session:
        assert await SettingsRepository.get(session, "first") is None
        assert await CoinRepository.enabled(session) == []
        assert await UserRepository.get(session, STRANGER_ID) is None


async def test_the_unique_constraint_on_orders_is_enforced(database: Database):
    row = dict(wallet=WALLET_A.lower(), oid=4242, coin="BTC", notional=1.0, status="open")
    async with database.session() as session:
        session.add(OrderRecord(**row))
    with pytest.raises(IntegrityError):
        async with database.session() as session:
            session.add(OrderRecord(**row))
    async with database.session() as session:
        found = (await session.execute(select(OrderRecord))).scalars().all()
    assert len(found) == 1


async def test_the_repository_upsert_avoids_that_collision(database: Database):
    """``OrderRepository.upsert`` is read-then-write, so it never conflicts."""
    for size in (40.0, 25.0):
        async with database.session() as session:
            await OrderRepository.upsert(
                session, WALLET_A, 4242, coin="BTC", notional=1.0, size=size, status="open"
            )
    async with database.session() as session:
        rows = (await session.execute(select(OrderRecord))).scalars().all()
    assert len(rows) == 1
    assert rows[0].size == 25.0


# ── settings persistence ───────────────────────────────────────

async def test_a_setting_is_written_as_json_with_the_author(database: Database):
    async with database.session() as session:
        await SettingsRepository.set(session, KEY_MIN_WHALE, "5000000.0", MAIN_ADMIN_ID)
    async with database.session() as session:
        row = await session.get(Setting, KEY_MIN_WHALE)
    assert row is not None
    assert row.value == "5000000.0"
    assert row.updated_by == MAIN_ADMIN_ID


async def test_setting_the_same_key_twice_updates_in_place(database: Database):
    async with database.session() as session:
        await SettingsRepository.set(session, "k", "one")
        await SettingsRepository.set(session, "k", "two", CO_ADMIN_ID)
    async with database.session() as session:
        assert await SettingsRepository.get(session, "k") == "two"
        assert len(await SettingsRepository.all(session)) == 1


async def test_a_setting_can_be_deleted(database: Database):
    async with database.session() as session:
        await SettingsRepository.set(session, "temp", "1")
    async with database.session() as session:
        await SettingsRepository.delete(session, "temp")
    async with database.session() as session:
        assert await SettingsRepository.get(session, "temp") is None


async def test_the_threshold_survives_a_restart(container, database, env):
    await container.settings.set_threshold(7_500_000, MAIN_ADMIN_ID)
    reborn = SettingsService(database, env)
    await reborn.load()
    assert reborn.config.min_whale_value == 7_500_000.0


async def test_the_monitoring_switch_survives_a_restart(container, database, env):
    """§13: /stopmonitor must still be in force after a redeploy."""
    await container.settings.set_monitoring(False, MAIN_ADMIN_ID)
    reborn = SettingsService(database, env)
    await reborn.load()
    assert reborn.config.monitoring_enabled is False


async def test_public_mode_survives_a_restart(container, database, env):
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    reborn = SettingsService(database, env)
    await reborn.load()
    assert reborn.config.public_mode is True


async def test_the_stored_value_beats_the_environment_default(container, database):
    """Env vars seed the first boot only; after that the database decides."""
    await container.settings.set_threshold(4_000_000, MAIN_ADMIN_ID)
    other_env = make_settings(min_whale_value=99_000_000.0, public_mode=True)
    reborn = SettingsService(database, other_env)
    await reborn.load()
    assert reborn.config.min_whale_value == 4_000_000.0
    assert reborn.config.public_mode is False


async def test_a_corrupt_stored_value_falls_back_to_the_default(database, env):
    async with database.session() as session:
        await SettingsRepository.set(session, KEY_MIN_WHALE, "not-json")
        await SettingsRepository.set(session, KEY_MONITORING, "{{{")
    service = SettingsService(database, env)
    await service.load()
    assert service.config.min_whale_value == env.min_whale_value
    assert service.config.monitoring_enabled is True


async def test_load_seeds_every_key_on_first_boot(database, env):
    service = SettingsService(database, env)
    await service.load()
    async with database.session() as session:
        stored = await SettingsRepository.all(session)
    for key in (KEY_MONITORING, KEY_PUBLIC_MODE, KEY_MIN_WHALE):
        assert key in stored


async def test_a_settings_change_writes_an_audit_row(container, database):
    await container.settings.set_threshold(3_000_000, MAIN_ADMIN_ID)
    async with database.session() as session:
        rows = await AuditRepository.recent(session)
    actions = [row.action for row in rows]
    assert f"set:{KEY_MIN_WHALE}" in actions
    entry = next(row for row in rows if row.action == f"set:{KEY_MIN_WHALE}")
    assert entry.admin_id == MAIN_ADMIN_ID
    assert entry.new_value == "3000000.0"


# ── coin persistence ───────────────────────────────────────────

async def test_the_coin_selection_survives_a_restart(container, database, env):
    await container.settings.set_coins(["btc", "hype"], MAIN_ADMIN_ID)
    reborn = SettingsService(database, env)
    await reborn.load()
    assert reborn.config.coins == ("BTC", "HYPE")


async def test_replace_uppercases_and_deduplicates(database: Database):
    async with database.session() as session:
        await CoinRepository.replace(session, ["btc", "BTC", "eth"], MAIN_ADMIN_ID)
    async with database.session() as session:
        assert await CoinRepository.enabled(session) == ["BTC", "ETH"]


async def test_adding_a_coin_is_idempotent(database: Database):
    async with database.session() as session:
        assert await CoinRepository.add(session, "sol", MAIN_ADMIN_ID) is True
    async with database.session() as session:
        assert await CoinRepository.add(session, "SOL") is False
    async with database.session() as session:
        assert await CoinRepository.enabled(session) == ["SOL"]


async def test_a_disabled_coin_is_re_enabled_rather_than_duplicated(database: Database):
    async with database.session() as session:
        await CoinRepository.add(session, "ARB")
        rows = await CoinRepository.all(session)
        rows[0].enabled = False
    async with database.session() as session:
        assert await CoinRepository.enabled(session) == []
        assert await CoinRepository.add(session, "arb") is True
    async with database.session() as session:
        assert await CoinRepository.enabled(session) == ["ARB"]
        assert len(await CoinRepository.all(session)) == 1


async def test_removing_an_unknown_coin_reports_false(database: Database):
    async with database.session() as session:
        assert await CoinRepository.remove(session, "NOPE") is False


# ── admin persistence ──────────────────────────────────────────

async def test_the_main_admin_row_is_created_on_load(container, database):
    async with database.session() as session:
        row = await AdminRepository.get(session, MAIN_ADMIN_ID)
    assert row is not None
    assert row.role == ROLE_MAIN


async def test_ensure_main_is_idempotent(database: Database):
    for _ in range(3):
        async with database.session() as session:
            await AdminRepository.ensure_main(session, MAIN_ADMIN_ID, "owner")
    async with database.session() as session:
        rows = await AdminRepository.list(session)
    assert len(rows) == 1
    assert rows[0].username == "owner"


async def test_a_co_admin_survives_a_redeploy(container, database, env):
    """§48: the admin list is exactly the kind of state RAM must not own."""
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")
    reborn = AdminService(database, env)
    await reborn.load()
    assert reborn.role_of(CO_ADMIN_ID) == ROLE_CO
    assert reborn.can(CO_ADMIN_ID, Capability.CONTROL_MONITORING) is True
    assert reborn.can(CO_ADMIN_ID, Capability.MANAGE_ADMINS) is False


async def test_removing_a_co_admin_persists(container, database, env):
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)
    await container.admins.remove_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)
    reborn = AdminService(database, env)
    await reborn.load()
    assert reborn.role_of(CO_ADMIN_ID) != ROLE_CO
    async with database.session() as session:
        assert await AdminRepository.get(session, CO_ADMIN_ID) is None


async def test_the_repository_refuses_to_delete_the_main_admin(container, database):
    """The protection is in the data layer too, not only in the service."""
    async with database.session() as session:
        assert await AdminRepository.remove(session, MAIN_ADMIN_ID) is False
    async with database.session() as session:
        row = await AdminRepository.get(session, MAIN_ADMIN_ID)
    assert row is not None and row.role == ROLE_MAIN


async def test_removing_an_absent_admin_reports_false(database: Database):
    async with database.session() as session:
        assert await AdminRepository.remove(session, 123_456_789) is False


async def test_add_co_admin_reports_not_created_the_second_time(database: Database):
    async with database.session() as session:
        _row, created = await AdminRepository.add_co_admin(session, CO_ADMIN_ID, MAIN_ADMIN_ID)
        assert created is True
    async with database.session() as session:
        _row, created = await AdminRepository.add_co_admin(session, CO_ADMIN_ID, MAIN_ADMIN_ID)
    assert created is False


async def test_add_co_admin_never_downgrades_the_main_admin(container, database):
    async with database.session() as session:
        row, created = await AdminRepository.add_co_admin(session, MAIN_ADMIN_ID, MAIN_ADMIN_ID)
    assert created is False
    assert row.role == ROLE_MAIN


async def test_roles_returns_an_id_keyed_map(container, database):
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)
    async with database.session() as session:
        roles = await AdminRepository.roles(session)
    assert roles == {MAIN_ADMIN_ID: ROLE_MAIN, CO_ADMIN_ID: ROLE_CO}
    assert all(isinstance(key, int) for key in roles)


async def test_the_audit_trail_records_admin_changes(container, database):
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)
    await container.admins.remove_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)
    async with database.session() as session:
        actions = {row.action for row in await AuditRepository.recent(session)}
    assert {"add:co_admin", "remove:co_admin"} <= actions


async def test_audit_values_are_truncated_not_rejected(database: Database):
    async with database.session() as session:
        await AuditRepository.record(
            session, MAIN_ADMIN_ID, "x" * 200, "t" * 300, "o" * 900, "n" * 900
        )
    async with database.session() as session:
        row = (await AuditRepository.recent(session))[0]
    assert len(row.action) == 48
    assert len(row.target) == 128
    assert len(row.old_value) == 512
    assert len(row.new_value) == 512


# ── users ──────────────────────────────────────────────────────

async def test_a_user_is_inserted_then_updated_in_place(database: Database):
    async with database.session() as session:
        await UserRepository.upsert(
            session, telegram_id=STRANGER_ID, chat_id=STRANGER_ID, username="a", first_name="A"
        )
    async with database.session() as session:
        await UserRepository.upsert(session, telegram_id=STRANGER_ID, chat_id=9999)
    async with database.session() as session:
        row = await UserRepository.get(session, STRANGER_ID)
        assert await UserRepository.count(session) == 1
    assert row.chat_id == 9999
    # An update that omits the username must not erase the known one.
    assert row.username == "a"
    assert row.first_name == "A"


async def test_a_returning_user_is_unblocked(database: Database):
    async with database.session() as session:
        await UserRepository.upsert(session, telegram_id=STRANGER_ID, chat_id=STRANGER_ID)
    async with database.session() as session:
        await UserRepository.mark_blocked(session, STRANGER_ID)
    async with database.session() as session:
        row = await UserRepository.get(session, STRANGER_ID)
        assert row.is_blocked is True and row.is_subscribed is False
    async with database.session() as session:
        await UserRepository.upsert(session, telegram_id=STRANGER_ID, chat_id=STRANGER_ID)
        row = await UserRepository.get(session, STRANGER_ID)
    assert row.is_blocked is False


async def test_subscribers_excludes_the_unsubscribed_and_the_blocked(database: Database):
    async with database.session() as session:
        for offset in range(3):
            await UserRepository.upsert(
                session, telegram_id=810_000 + offset, chat_id=810_000 + offset
            )
    async with database.session() as session:
        await UserRepository.set_subscribed(session, 810_001, False)
        await UserRepository.mark_blocked(session, 810_002)
    async with database.session() as session:
        ids = [row.telegram_id for row in await UserRepository.subscribers(session)]
    assert ids == [810_000]


async def test_alert_counters_increment(database: Database):
    async with database.session() as session:
        await UserRepository.upsert(session, telegram_id=STRANGER_ID, chat_id=STRANGER_ID)
    for _ in range(3):
        async with database.session() as session:
            await UserRepository.bump_alert_counts(session, [STRANGER_ID])
    async with database.session() as session:
        row = await UserRepository.get(session, STRANGER_ID)
    assert row.alerts_received == 3


async def test_bumping_an_empty_list_is_a_no_op(database: Database):
    async with database.session() as session:
        await UserRepository.bump_alert_counts(session, [])
        assert await UserRepository.count(session) == 0


# ── wallets ────────────────────────────────────────────────────

async def test_wallet_activity_aggregates_across_sessions(database: Database):
    async with database.session() as session:
        await WalletRepository.record_activity(
            session,
            WALLET_A.upper(),
            coin="BTC",
            side="LONG",
            notional=3_000_000.0,
            position_value=6_000_000.0,
            account_value=12_000_000.0,
        )
    async with database.session() as session:
        await WalletRepository.record_activity(
            session,
            WALLET_A,
            coin="BTC",
            side="SHORT",
            notional=1_000_000.0,
            order_value=9_000_000.0,
            position_value=2_000_000.0,
        )
    async with database.session() as session:
        row = await WalletRepository.get(session, WALLET_A)
        assert await WalletRepository.count(session) == 1

    assert row.address == WALLET_A.lower()          # case never forks a row
    assert row.event_count == 2
    assert row.total_notional == 4_000_000.0
    assert row.long_volume == 3_000_000.0
    assert row.short_volume == 1_000_000.0
    assert row.largest_position == 6_000_000.0      # a max, not the latest
    assert row.largest_order == 9_000_000.0
    assert row.account_value == 12_000_000.0
    assert row.coins == {"BTC": 2}


async def test_top_wallets_are_ordered_by_notional(database: Database):
    async with database.session() as session:
        await WalletRepository.record_activity(session, WALLET_A, notional=1_000_000.0)
        await WalletRepository.record_activity(session, WALLET_B, notional=9_000_000.0)
        await WalletRepository.record_activity(session, WALLET_C, notional=5_000_000.0)
    async with database.session() as session:
        top = await WalletRepository.top(session, limit=2)
    assert [row.address for row in top] == [WALLET_B.lower(), WALLET_C.lower()]


async def test_a_watched_wallet_survives_a_restart(container, database, env):
    await container.settings.add_tracked_wallet(WALLET_A.upper(), MAIN_ADMIN_ID, label="desk")
    reborn = SettingsService(database, env)
    await reborn.load()
    assert reborn.config.tracked_wallets == (WALLET_A.lower(),)


async def test_watching_the_same_wallet_twice_only_relabels(database: Database):
    async with database.session() as session:
        assert await WalletRepository.add_tracked(session, WALLET_A, MAIN_ADMIN_ID, "first") is True
    async with database.session() as session:
        assert await WalletRepository.add_tracked(session, WALLET_A, MAIN_ADMIN_ID, "second") is False
    async with database.session() as session:
        rows = await WalletRepository.tracked(session)
    assert len(rows) == 1
    assert rows[0].label == "second"


async def test_unwatching_removes_the_row(database: Database):
    async with database.session() as session:
        await WalletRepository.add_tracked(session, WALLET_A, MAIN_ADMIN_ID)
    async with database.session() as session:
        assert await WalletRepository.remove_tracked(session, WALLET_A.upper()) is True
    async with database.session() as session:
        assert await WalletRepository.tracked(session) == []
        assert await WalletRepository.remove_tracked(session, WALLET_A) is False


# ── whale events ───────────────────────────────────────────────

def event_row(**overrides):
    row = {
        "event_type": "WHALE_TRADE",
        "coin": "BTC",
        "side": "LONG",
        "wallet": WALLET_A.lower(),
        "notional": 3_000_000.0,
        "value_kind": "TRADE_VALUE",
        "detail": {},
        "dedup_key": "trade:btc:1",
        "event_time": utc_now(),
    }
    row.update(overrides)
    return row


async def test_an_event_is_persisted_with_an_id(database: Database):
    async with database.session() as session:
        row = await EventRepository.insert(session, **event_row())
        event_id = row.id
    assert event_id is not None
    async with database.session() as session:
        stored = await EventRepository.recent(session)
    assert len(stored) == 1
    assert stored[0].id == event_id
    assert stored[0].alerted is False


async def test_mark_alerted_flips_the_flag(database: Database):
    async with database.session() as session:
        row = await EventRepository.insert(session, **event_row())
        event_id = row.id
    async with database.session() as session:
        await EventRepository.mark_alerted(session, event_id)
    async with database.session() as session:
        assert (await EventRepository.recent(session))[0].alerted is True


async def test_seen_recently_is_bounded_by_the_window(database: Database):
    async with database.session() as session:
        await EventRepository.insert(session, **event_row(dedup_key="k1"))
    async with database.session() as session:
        assert await EventRepository.seen_recently(session, "k1", ago(60)) is True
        assert await EventRepository.seen_recently(session, "k1", utc_now() + timedelta(60)) is False
        assert await EventRepository.seen_recently(session, "other", ago(60)) is False


async def test_recent_events_are_newest_first_and_filterable(database: Database):
    async with database.session() as session:
        await EventRepository.insert(
            session,
            **event_row(dedup_key="old", event_time=ago(600), coin="ETH", notional=2_500_000.0),
        )
        await EventRepository.insert(
            session,
            **event_row(dedup_key="new", event_time=ago(10), coin="BTC", notional=9_000_000.0),
        )
        await EventRepository.insert(
            session,
            **event_row(
                dedup_key="order",
                event_time=ago(20),
                event_type="ORDER_PLACED",
                wallet=WALLET_B.lower(),
                notional=4_000_000.0,
            ),
        )
    async with database.session() as session:
        assert [e.dedup_key for e in await EventRepository.recent(session)] == [
            "new",
            "order",
            "old",
        ]
        assert [e.dedup_key for e in await EventRepository.recent(session, coin="btc")] == [
            "new",
            "order",
        ]
        assert [
            e.dedup_key for e in await EventRepository.recent(session, event_types=["ORDER_PLACED"])
        ] == ["order"]
        assert [
            e.dedup_key for e in await EventRepository.recent(session, wallet=WALLET_B.upper())
        ] == ["order"]
        assert [
            e.dedup_key for e in await EventRepository.recent(session, min_notional=5_000_000.0)
        ] == ["new"]
        assert [e.dedup_key for e in await EventRepository.recent(session, since=ago(30))] == [
            "new",
            "order",
        ]
        assert len(await EventRepository.recent(session, limit=1)) == 1


async def test_the_summary_aggregates_what_the_panel_shows(database: Database):
    async with database.session() as session:
        await EventRepository.insert(
            session, **event_row(dedup_key="a", notional=3_000_000.0, side="LONG", coin="BTC")
        )
        await EventRepository.insert(
            session, **event_row(dedup_key="b", notional=8_000_000.0, side="SHORT", coin="BTC")
        )
        await EventRepository.insert(
            session,
            **event_row(
                dedup_key="c",
                notional=2_000_000.0,
                side="BUY",
                coin="ETH",
                event_type="ORDER_PLACED",
            ),
        )
    async with database.session() as session:
        summary = await EventRepository.summary(session)
    assert summary["total"] == 3
    assert summary["notional"] == 13_000_000.0
    assert summary["largest"] == 8_000_000.0
    assert summary["by_coin"][0] == {"coin": "BTC", "count": 2, "notional": 11_000_000.0}
    assert {item["type"] for item in summary["by_type"]} == {"WHALE_TRADE", "ORDER_PLACED"}
    # The repository draws no conclusions about which rows are trades; it reports
    # the grouped facts the whale layer needs to split them.
    assert {(r["type"], r["side"], r["count"]) for r in summary["by_type_side"]} == {
        ("WHALE_TRADE", "LONG", 1),
        ("WHALE_TRADE", "SHORT", 1),
        ("ORDER_PLACED", "BUY", 1),
    }


async def test_the_split_counts_executions_apart_from_order_events(database: Database):
    """Task D — a placed order is not a trade, and never joins the trade totals."""
    async with database.session() as session:
        await EventRepository.insert(
            session, **event_row(dedup_key="a", notional=3_000_000.0, side="BUY", coin="BTC")
        )
        await EventRepository.insert(
            session,
            **event_row(
                dedup_key="b",
                notional=9_000_000.0,
                side="SELL",
                coin="BTC",
                event_type="ORDER_PLACED",
            ),
        )
        await EventRepository.insert(
            session,
            **event_row(
                dedup_key="c",
                notional=4_000_000.0,
                side="LONG",
                coin="ETH",
                event_type="POSITION_OPENED",
            ),
        )
    async with database.session() as session:
        split = summarize_events(await EventRepository.summary(session))

    assert split["executions"] == 1
    assert split["execution_notional"] == 3_000_000.0
    assert split["largest_execution"] == 3_000_000.0
    assert split["order_events"] == 1
    assert split["order_notional"] == 9_000_000.0
    assert split["position_events"] == 1
    # The $9M *intended* by the resting order is nowhere in the executed figures.
    assert split["execution_notional"] != split["notional"]
    # BUY/SELL counts executions; LONG/SHORT counts verified positions. The resting
    # SELL is an intention and appears in neither.
    assert (split["buys"], split["sells"]) == (1, 0)
    assert (split["longs"], split["shorts"]) == (1, 0)
    assert split["total"] == 3


async def test_an_empty_summary_returns_zeros_not_none(database: Database):
    async with database.session() as session:
        summary = await EventRepository.summary(session, since=ago(60))
    assert summary["total"] == 0
    assert summary["notional"] == 0.0
    assert summary["largest"] == 0.0
    assert summary["by_coin"] == []
    assert summary["by_type_side"] == []
    split = summarize_events(summary)
    assert (split["executions"], split["order_events"], split["position_events"]) == (0, 0, 0)
    assert split["execution_notional"] == 0.0


async def test_the_wallet_leaderboard_counts_executions_not_order_churn(database: Database):
    """Task C — a wallet that places and cancels an order has made no trades."""
    async with database.session() as session:
        # WALLET_A: one $3M execution, plus a placed-then-cancelled $50M order.
        await EventRepository.insert(
            session, **event_row(dedup_key="a1", wallet=WALLET_A, notional=3_000_000.0, coin="BTC")
        )
        for index, event_type in enumerate(("ORDER_PLACED", "ORDER_CANCELLED")):
            await EventRepository.insert(
                session,
                **event_row(
                    dedup_key=f"a-order-{index}",
                    wallet=WALLET_A,
                    notional=50_000_000.0,
                    coin="BTC",
                    event_type=event_type,
                ),
            )
        # WALLET_B: two real executions worth $6M.
        for index in range(2):
            await EventRepository.insert(
                session,
                **event_row(
                    dedup_key=f"b{index}",
                    wallet=WALLET_B,
                    notional=3_000_000.0,
                    coin="ETH",
                ),
            )
    async with database.session() as session:
        board = await EventRepository.wallet_leaderboard(session, EXECUTION_TYPE_NAMES)

    by_wallet = {entry["address"]: entry for entry in board}
    assert by_wallet[WALLET_A.lower()]["trades"] == 1
    assert by_wallet[WALLET_A.lower()]["volume"] == 3_000_000.0
    assert by_wallet[WALLET_B.lower()]["trades"] == 2
    # Executed volume ranks the board, so the order churn does not put A on top.
    assert board[0]["address"] == WALLET_B.lower()
    assert by_wallet[WALLET_A.lower()]["coins"] == ["BTC"]
    # Addresses are stored and returned in full — never a prefix.
    assert all(len(entry["address"]) == 42 for entry in board)


async def test_the_wallet_leaderboard_is_empty_without_types(database: Database):
    async with database.session() as session:
        assert await EventRepository.wallet_leaderboard(session, []) == []


async def test_pruning_deletes_only_the_old_rows(database: Database):
    async with database.session() as session:
        await EventRepository.insert(
            session, **event_row(dedup_key="stale", created_at=ago(86_400 * 40))
        )
        await EventRepository.insert(session, **event_row(dedup_key="fresh"))
    async with database.session() as session:
        removed = await EventRepository.prune(session, utc_now() - timedelta(days=30))
    assert removed == 1
    async with database.session() as session:
        assert [e.dedup_key for e in await EventRepository.recent(session)] == ["fresh"]


# ── orders ─────────────────────────────────────────────────────

async def test_an_order_upsert_reports_the_previous_status(database: Database):
    async with database.session() as session:
        _row, previous = await OrderRepository.upsert(
            session, WALLET_A, 500, coin="BTC", notional=4_000_000.0, status="open"
        )
        assert previous is None
    async with database.session() as session:
        _row, previous = await OrderRepository.upsert(session, WALLET_A, 500, status="canceled")
    assert previous == "open"
    async with database.session() as session:
        row = await OrderRepository.get(session, WALLET_A.upper(), 500)
    assert row.status == "canceled"
    assert row.notional == 4_000_000.0     # a None-free partial update keeps the rest


async def test_an_order_upsert_ignores_none_fields(database: Database):
    async with database.session() as session:
        await OrderRepository.upsert(
            session, WALLET_A, 501, coin="BTC", notional=4_000_000.0, size=40.0, status="open"
        )
    async with database.session() as session:
        await OrderRepository.upsert(session, WALLET_A, 501, size=None, status="open")
    async with database.session() as session:
        row = await OrderRepository.get(session, WALLET_A, 501)
    assert row.size == 40.0


async def test_open_orders_are_ranked_by_notional_and_filterable(database: Database):
    async with database.session() as session:
        await OrderRepository.upsert(
            session, WALLET_A, 1, coin="BTC", notional=3_000_000.0, status="open"
        )
        await OrderRepository.upsert(
            session, WALLET_A, 2, coin="BTC", notional=9_000_000.0, status="open"
        )
        await OrderRepository.upsert(
            session, WALLET_B, 3, coin="ETH", notional=7_000_000.0, status="open"
        )
        await OrderRepository.upsert(
            session, WALLET_B, 4, coin="BTC", notional=20_000_000.0, status="canceled"
        )
    async with database.session() as session:
        assert [r.oid for r in await OrderRepository.open_orders(session)] == [2, 3, 1]
        assert [r.oid for r in await OrderRepository.open_orders(session, coin="eth")] == [3]
        assert [
            r.oid for r in await OrderRepository.open_orders(session, min_notional=5_000_000.0)
        ] == [2, 3]
        assert [r.oid for r in await OrderRepository.open_orders(session, limit=1)] == [2]


async def test_closing_an_order_stamps_the_time(database: Database):
    async with database.session() as session:
        await OrderRepository.upsert(
            session, WALLET_A, 600, coin="BTC", notional=1.0, status="open"
        )
    async with database.session() as session:
        await OrderRepository.close(session, WALLET_A.upper(), 600, "canceled")
    async with database.session() as session:
        row = await OrderRepository.get(session, WALLET_A, 600)
    assert row.status == "canceled"
    assert row.closed_at is not None


# ── positions ──────────────────────────────────────────────────

async def test_a_position_is_keyed_by_wallet_and_coin(database: Database):
    async with database.session() as session:
        await PositionRepository.upsert(
            session, WALLET_A.upper(), "btc", side="LONG", size=60.0, position_value=6_000_000.0
        )
    async with database.session() as session:
        await PositionRepository.upsert(session, WALLET_A, "BTC", size=90.0)
    async with database.session() as session:
        row = await PositionRepository.get(session, WALLET_A, "BTC")
        others = await PositionRepository.by_wallet(session, WALLET_A)
    assert row.wallet == WALLET_A.lower() and row.coin == "BTC"
    assert row.size == 90.0
    assert row.side == "LONG"
    assert len(others) == 1


async def test_a_closed_position_leaves_the_open_list(database: Database):
    async with database.session() as session:
        await PositionRepository.upsert(
            session, WALLET_A, "BTC", position_value=6_000_000.0, is_open=True
        )
        await PositionRepository.upsert(
            session, WALLET_B, "ETH", position_value=9_000_000.0, is_open=True
        )
    async with database.session() as session:
        assert [r.coin for r in await PositionRepository.open_positions(session)] == ["ETH", "BTC"]
        await PositionRepository.upsert(session, WALLET_B, "ETH", is_open=False)
    async with database.session() as session:
        assert [r.coin for r in await PositionRepository.open_positions(session)] == ["BTC"]
        assert await PositionRepository.by_wallet(session, WALLET_B) == []


async def test_open_positions_filters_by_coin_and_size(database: Database):
    async with database.session() as session:
        await PositionRepository.upsert(session, WALLET_A, "BTC", position_value=1_000_000.0)
        await PositionRepository.upsert(session, WALLET_B, "BTC", position_value=8_000_000.0)
        await PositionRepository.upsert(session, WALLET_C, "SOL", position_value=5_000_000.0)
    async with database.session() as session:
        assert len(await PositionRepository.open_positions(session, coin="btc")) == 2
        assert [
            r.coin
            for r in await PositionRepository.open_positions(session, min_notional=4_000_000.0)
        ] == ["BTC", "SOL"]


# ── alert history ──────────────────────────────────────────────

async def test_a_delivered_alert_is_recorded(database: Database):
    async with database.session() as session:
        await AlertRepository.record(
            session, "key-1", event_id=7, chat_id=MAIN_ADMIN_ID, message_id=42
        )
    async with database.session() as session:
        assert await AlertRepository.sent_since(session, "key-1", ago(60)) is True
        assert await AlertRepository.count_since(session, ago(60)) == 1


async def test_a_failed_delivery_does_not_count_as_sent(database: Database):
    async with database.session() as session:
        await AlertRepository.record(session, "key-2", ok=False, error="x" * 400)
    async with database.session() as session:
        assert await AlertRepository.sent_since(session, "key-2", ago(60)) is False
        assert await AlertRepository.recent_keys(session, ago(60)) == []
        assert await AlertRepository.count_since(session, ago(60)) == 1


async def test_recent_keys_warm_the_dedup_cache_after_a_restart(database: Database):
    """§48: a redeploy must not re-announce alerts that already went out."""
    async with database.session() as session:
        for index in range(5):
            await AlertRepository.record(session, f"warm-{index}")
    async with database.session() as session:
        keys = await AlertRepository.recent_keys(session, ago(600))
        limited = await AlertRepository.recent_keys(session, ago(600), limit=2)
    assert set(keys) == {f"warm-{i}" for i in range(5)}
    assert len(limited) == 2


# ── operational logs ───────────────────────────────────────────

async def test_a_log_line_is_persisted_and_truncated(database: Database):
    async with database.session() as session:
        await LogRepository.write(
            session, "INFO" * 10, "source" * 40, "m" * 900, {"coin": "BTC"}
        )
    async with database.session() as session:
        row = (await session.execute(select(BotLog))).scalars().one()
    assert len(row.level) == 16
    assert len(row.source) == 64
    assert len(row.message) == 512
    assert row.context == {"coin": "BTC"}


async def test_pruning_logs_keeps_the_recent_window(database: Database):
    async with database.session() as session:
        session.add(
            BotLog(
                level="INFO",
                source="test",
                message="ancient",
                context={},
                created_at=utc_now() - timedelta(days=40),
            )
        )
        await LogRepository.write(session, "INFO", "test", "fresh")
    async with database.session() as session:
        assert await LogRepository.prune(session, keep_days=14) == 1
    async with database.session() as session:
        rows = (await session.execute(select(BotLog))).scalars().all()
    assert [row.message for row in rows] == ["fresh"]


# ── the whole-deployment guarantee ─────────────────────────────

async def test_a_redeploy_restores_every_administrator_decision(container, database, env):
    """§48/§49 end to end: one container writes, a brand new one reads it back.

    This is the closest offline equivalent of a Railway redeploy — same
    PostgreSQL/SQLite database, a completely fresh process-level cache.
    """
    await container.settings.set_threshold(7_500_000, MAIN_ADMIN_ID)
    await container.settings.set_cooldown(90, MAIN_ADMIN_ID)
    await container.settings.set_monitoring(False, MAIN_ADMIN_ID)
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    await container.settings.set_coins(["btc", "hype"], MAIN_ADMIN_ID)
    await container.settings.toggle("enable_book_scanner", MAIN_ADMIN_ID)
    await container.settings.add_tracked_wallet(WALLET_C, MAIN_ADMIN_ID, label="desk")
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")

    redeployed = AppContainer(env, database)
    await redeployed.restore()
    try:
        config = redeployed.settings.config
        assert config.min_whale_value == 7_500_000.0
        assert config.alert_cooldown_seconds == 90
        assert config.monitoring_enabled is False
        assert config.public_mode is True
        assert config.coins == ("BTC", "HYPE")
        assert config.enable_book_scanner is True
        assert config.tracked_wallets == (WALLET_C.lower(),)
        assert redeployed.admins.role_of(CO_ADMIN_ID) == ROLE_CO
        assert redeployed.admins.role_of(MAIN_ADMIN_ID) == ROLE_MAIN
        assert redeployed.admins.co_admin_count == 1
    finally:
        await redeployed.alerts.stop()
