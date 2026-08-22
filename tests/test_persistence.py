"""Persistence and settings safety.

Two production failures drive this file:

1. changing one setting replaced the others — an "add a coin" that wiped the
   coin list;
2. configuration disappeared after a Railway redeploy.

So every test here asserts one of two things. Either **a change is a patch** —
after it, everything the caller did not name is untouched — or **a value
survives a new process** reading the same database. The redeploy simulation is a
brand-new :class:`AppContainer` over the same ``Database``: fresh caches, fresh
services, same rows, which is exactly what Railway does to a container.

Spec sections: §1–§5 (patch semantics), §6–§9 (durability), §11/§12 (defaults
only where nothing exists), §14 (roles survive), §22/§23 (nothing is removed
without an explicit remove/reset), §27 (/config), §28, §29, §33.
"""

from __future__ import annotations

from app.bot.handlers import admin as admin_cmds
from app.bot.handlers import prompts
from app.bot.handlers.callbacks import on_callback
from app.container import AppContainer
from app.database.repository import (
    AdminRepository,
    CoinRepository,
    SettingsRepository,
    WalletRepository,
)
from app.services.admin_service import ROLE_CO, ROLE_MAIN, Capability
from app.services.settings_service import (
    KEY_BOOTSTRAPPED,
    KEY_MIN_WHALE,
    SettingsService,
)
from tests.conftest import CO_ADMIN_ID, MAIN_ADMIN_ID, STRANGER_ID, FakeUpdate

WALLET = "0x31dea2516beee92135b96f464eeec3cf292a13f2"


async def redeploy(env, database) -> AppContainer:
    """A fresh process over the same database — the offline Railway redeploy."""
    reborn = AppContainer(env, database)
    await reborn.restore()
    return reborn


# ── 1. a coin addition is a patch ──────────────────────────────

async def test_addcoin_keeps_the_existing_coins(container, ctx):
    """§2: /addcoin HYPE on BTC/ETH/SOL must give BTC ETH SOL HYPE, not HYPE."""
    await container.settings.set_coins(["BTC", "ETH", "SOL"], MAIN_ADMIN_ID)
    ctx.args = ["HYPE"]
    await admin_cmds.cmd_addcoin(FakeUpdate(MAIN_ADMIN_ID, text="/addcoin HYPE"), ctx)
    assert container.settings.config.coins == ("BTC", "ETH", "HYPE", "SOL")


async def test_addcoin_twice_creates_no_duplicate(container, ctx, database):
    """§3: the second /addcoin HYPE is a no-op, and says so."""
    await container.settings.set_coins(["BTC"], MAIN_ADMIN_ID)
    ctx.args = ["HYPE"]
    await admin_cmds.cmd_addcoin(FakeUpdate(MAIN_ADMIN_ID, text="/addcoin HYPE"), ctx)
    second = FakeUpdate(MAIN_ADMIN_ID, text="/addcoin HYPE")
    ctx.args = ["HYPE"]
    await admin_cmds.cmd_addcoin(second, ctx)

    assert container.settings.config.coins == ("BTC", "HYPE")
    async with database.session() as session:
        rows = [row.coin for row in await CoinRepository.all(session)]
    assert rows.count("HYPE") == 1
    assert "already monitored" in " ".join(second.sent)


async def test_the_add_coins_prompt_adds_instead_of_replacing(container, ctx):
    """§4/§34 — the root cause. The ➕ Add coins prompt used to call set_coins."""
    await container.settings.set_coins(["BTC", "ETH", "SOL"], MAIN_ADMIN_ID)
    ctx.user_data["pending"] = {"kind": "coins"}
    await prompts.on_text(FakeUpdate(MAIN_ADMIN_ID, text="HYPE"), ctx)
    assert container.settings.config.coins == ("BTC", "ETH", "HYPE", "SOL")


async def test_the_coin_toggle_button_behaves_like_the_command(container, ctx):
    """§4: a button is a command. Adding one coin touches no other coin."""
    await container.settings.set_coins(["BTC", "ETH", "SOL"], MAIN_ADMIN_ID)
    await on_callback(FakeUpdate(MAIN_ADMIN_ID, callback_data="coin:toggle:HYPE"), ctx)
    assert container.settings.config.coins == ("BTC", "ETH", "HYPE", "SOL")


# ── 2. one setting at a time ───────────────────────────────────

async def test_changing_the_threshold_does_not_reset_the_coins(container, ctx):
    """§2: /threshold 5000000 must not touch the coin list."""
    await container.settings.set_coins(["BTC", "ETH", "SOL"], MAIN_ADMIN_ID)
    ctx.args = ["5000000"]
    await admin_cmds.cmd_setthreshold(FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 5m"), ctx)
    config = container.settings.config
    assert config.min_whale_value == 5_000_000.0
    assert config.coins == ("BTC", "ETH", "SOL")


async def test_changing_one_setting_leaves_every_other_field_alone(container):
    """§1/§19: each setter updates only its own state."""
    await container.settings.set_coins(["BTC", "HYPE"], MAIN_ADMIN_ID)
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    await container.settings.set_cooldown(90, MAIN_ADMIN_ID)
    await container.settings.add_tracked_wallet(WALLET, MAIN_ADMIN_ID)
    before = container.settings.config

    await container.settings.set_threshold(4_000_000, MAIN_ADMIN_ID)

    after = container.settings.config
    assert after.min_whale_value == 4_000_000.0
    for field in (
        "coins",
        "public_mode",
        "alert_cooldown_seconds",
        "tracked_wallets",
        "all_coins",
        "monitoring_enabled",
        "paused",
        "enable_trade_detector",
        "enable_position_detector",
        "enable_order_alerts",
        "min_margin_value",
    ):
        assert getattr(after, field) == getattr(before, field), field


async def test_toggling_one_alert_switch_leaves_the_others(container, ctx):
    """§5: a panel must not submit every visible value as a replacement."""
    before = container.settings.config
    await on_callback(
        FakeUpdate(MAIN_ADMIN_ID, callback_data="set:toggle:enable_book_scanner"), ctx
    )
    after = container.settings.config
    assert after.enable_book_scanner is not before.enable_book_scanner
    assert after.enable_trade_detector == before.enable_trade_detector
    assert after.enable_position_detector == before.enable_position_detector
    assert after.enable_order_cancel_alerts == before.enable_order_cancel_alerts
    assert after.coins == before.coins


# ── 3. durability across a restart and a redeploy ──────────────

async def test_a_coin_added_then_a_threshold_change_both_survive_a_redeploy(
    container, database, env
):
    """§9/§33 — the acceptance scenario, offline.

    Add HYPE, change the threshold, then start a completely new container over
    the same database twice, as a restart followed by a redeploy would.
    """
    await container.settings.set_coins(["BTC", "ETH", "SOL"], MAIN_ADMIN_ID)
    await container.settings.add_coin("HYPE", MAIN_ADMIN_ID)
    await container.settings.set_threshold(5_000_000, MAIN_ADMIN_ID)

    restarted = await redeploy(env, database)
    assert restarted.settings.config.coins == ("BTC", "ETH", "HYPE", "SOL")
    assert restarted.settings.config.min_whale_value == 5_000_000.0

    redeployed = await redeploy(env, database)
    assert redeployed.settings.config.coins == ("BTC", "ETH", "HYPE", "SOL")
    assert redeployed.settings.config.min_whale_value == 5_000_000.0
    assert redeployed.settings.first_boot is False


async def test_public_mode_survives_a_redeploy(container, database, env):
    """§15."""
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    assert (await redeploy(env, database)).settings.config.public_mode is True


async def test_watched_wallets_survive_a_redeploy(container, database, env):
    """§16."""
    await container.settings.add_tracked_wallet(WALLET, MAIN_ADMIN_ID, label="desk")
    reborn = await redeploy(env, database)
    assert reborn.settings.config.tracked_wallets == (WALLET,)
    async with database.session() as session:
        rows = await WalletRepository.tracked(session)
    # §21 of the earlier task: the stored address is never truncated.
    assert rows[0].address == WALLET


async def test_a_co_admin_is_still_a_co_admin_after_a_redeploy(container, database, env):
    """§14: a redeploy must not turn co-admins into normal users."""
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")
    reborn = await redeploy(env, database)
    assert reborn.admins.role_of(CO_ADMIN_ID) == ROLE_CO
    assert reborn.admins.role_of(MAIN_ADMIN_ID) == ROLE_MAIN
    assert reborn.admins.role_of(STRANGER_ID) not in (ROLE_CO, ROLE_MAIN)
    async with database.session() as session:
        roles = await AdminRepository.roles(session)
    assert roles[CO_ADMIN_ID] == ROLE_CO


async def test_the_global_pause_survives_a_redeploy(container, database, env):
    """A paused deployment must come back paused, not silently resume."""
    await container.settings.set_paused(True, MAIN_ADMIN_ID)
    assert (await redeploy(env, database)).settings.config.paused is True


async def test_alert_toggles_survive_a_redeploy(container, database, env):
    await container.settings.set_value("enable_trade_detector", False, MAIN_ADMIN_ID)
    await container.settings.set_value("enable_order_alerts", True, MAIN_ADMIN_ID)
    config = (await redeploy(env, database)).settings.config
    assert config.enable_trade_detector is False
    assert config.enable_order_alerts is True
    assert config.enable_position_detector is True


# ── 4. defaults only where nothing exists ──────────────────────

async def test_startup_does_not_overwrite_a_stored_value_with_a_default(
    container, database, env
):
    """§11/§12: a startup UPSERT must only initialise missing fields."""
    await container.settings.set_threshold(9_000_000, MAIN_ADMIN_ID)
    assert env.min_whale_value == 2_000_000.0  # the default it must not restore

    reborn = SettingsService(database, env)
    await reborn.load()
    assert reborn.config.min_whale_value == 9_000_000.0

    async with database.session() as session:
        stored = await SettingsRepository.all(session)
    assert SettingsService.decode_stored(stored[KEY_MIN_WHALE]) == 9_000_000.0


async def test_an_empty_coin_list_is_not_re_seeded_on_the_next_start(
    container, database, env
):
    """§11: an admin who monitors nothing must not find DEFAULT_COINS restored."""
    await container.settings.set_coins([], MAIN_ADMIN_ID)
    assert container.settings.config.coins == ()
    reborn = await redeploy(env, database)
    assert reborn.settings.config.coins == ()


async def test_the_first_boot_marker_is_recorded_once(container, database, env):
    """The "are we bootstrapped?" question is answered by a row, not a guess."""
    async with database.session() as session:
        stored = await SettingsRepository.all(session)
    assert KEY_BOOTSTRAPPED in stored
    stamp = SettingsService.decode_stored(stored[KEY_BOOTSTRAPPED])
    assert stamp

    reborn = await redeploy(env, database)
    assert reborn.settings.bootstrapped_at == stamp
    assert reborn.settings.first_boot is False


async def test_defaults_are_seeded_when_the_database_is_empty(database, env):
    """The other half of §11: with nothing stored, the environment does apply."""
    service = SettingsService(database, env)
    await service.load()
    assert service.first_boot is True
    assert service.config.coins == ("BTC", "ETH", "SOL")
    assert service.config.min_whale_value == 2_000_000.0


# ── 5. nothing is removed without an explicit remove ───────────

async def test_setcoins_replaces_but_reports_what_it_removed(container, ctx):
    """§22: /setcoins may remove — never silently."""
    await container.settings.set_coins(["BTC", "ETH", "SOL"], MAIN_ADMIN_ID)
    ctx.args = ["BTC", "HYPE"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setcoins BTC HYPE")
    await admin_cmds.cmd_setcoins(update, ctx)
    assert container.settings.config.coins == ("BTC", "HYPE")
    said = " ".join(update.sent)
    assert "Removed" in said and "ETH" in said and "SOL" in said
    assert "Added" in said


async def test_replace_only_touches_the_rows_that_change(container, database):
    """§13/§20: adding HYPE must not rewrite the BTC row."""
    await container.settings.set_coins(["BTC", "ETH"], MAIN_ADMIN_ID)
    async with database.session() as session:
        before = {row.coin: row.created_at for row in await CoinRepository.all(session)}

    async with database.session() as session:
        added, removed = await CoinRepository.replace(
            session, ["BTC", "ETH", "HYPE"], MAIN_ADMIN_ID
        )
    assert added == ["HYPE"]
    assert removed == []

    async with database.session() as session:
        after = {row.coin: row.created_at for row in await CoinRepository.all(session)}
    assert after["BTC"] == before["BTC"]
    assert after["ETH"] == before["ETH"]


async def test_clearing_the_coin_list_requires_confirmation(container, ctx):
    """§22/§23: the first tap only asks."""
    await container.settings.set_coins(["BTC", "ETH", "SOL"], MAIN_ADMIN_ID)
    ask = FakeUpdate(MAIN_ADMIN_ID, callback_data="coin:clear")
    await on_callback(ask, ctx)
    assert container.settings.config.coins == ("BTC", "ETH", "SOL")
    assert "Clear the coin list?" in " ".join(ask.sent)

    confirm = FakeUpdate(MAIN_ADMIN_ID, callback_data="coin:clearyes")
    await on_callback(confirm, ctx)
    assert container.settings.config.coins == ()
    assert "Cleared" in " ".join(confirm.sent)


async def test_resetsettings_alone_changes_nothing(container, ctx):
    """§23: an ordinary invocation must never perform the reset."""
    await container.settings.set_threshold(7_000_000, MAIN_ADMIN_ID)
    await container.settings.set_coins(["HYPE"], MAIN_ADMIN_ID)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/resetsettings")
    await admin_cmds.cmd_resetsettings(update, ctx)
    assert container.settings.config.min_whale_value == 7_000_000.0
    assert container.settings.config.coins == ("HYPE",)
    assert "Reset all settings" in " ".join(update.sent)


async def test_a_confirmed_reset_restores_defaults_but_keeps_records(
    container, ctx, database, env
):
    """§22: a settings reset is not a data reset."""
    await container.settings.set_threshold(7_000_000, MAIN_ADMIN_ID)
    await container.settings.set_coins(["HYPE"], MAIN_ADMIN_ID)
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    await container.settings.add_tracked_wallet(WALLET, MAIN_ADMIN_ID)
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")

    await on_callback(FakeUpdate(MAIN_ADMIN_ID, callback_data="reset:confirm"), ctx)

    config = container.settings.config
    assert config.min_whale_value == 2_000_000.0
    assert config.coins == ("BTC", "ETH", "SOL")
    assert config.public_mode is False
    # Records, not preferences: these survive the reset.
    assert config.tracked_wallets == (WALLET,)
    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_CO

    # And the reset itself is persisted, not only cached.
    reborn = await redeploy(env, database)
    assert reborn.settings.config.min_whale_value == 2_000_000.0
    assert reborn.settings.config.coins == ("BTC", "ETH", "SOL")


async def test_a_co_admin_cannot_reset_the_settings(container, ctx):
    """A forged ``reset:confirm`` is refused server-side, like every callback."""
    await container.admins.add_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID, username="helper")
    await container.settings.set_threshold(7_000_000, MAIN_ADMIN_ID)

    update = FakeUpdate(CO_ADMIN_ID, callback_data="reset:confirm")
    await on_callback(update, ctx)

    assert container.settings.config.min_whale_value == 7_000_000.0
    assert container.admins.can(CO_ADMIN_ID, Capability.RESET_SETTINGS) is False
    assert any("Main Admin" in text for text in update.sent)


# ── 6. /config reports the database, not the cache ─────────────

async def test_config_reads_the_stored_rows(container, ctx):
    """§27."""
    await container.settings.set_coins(["BTC", "HYPE"], MAIN_ADMIN_ID)
    await container.settings.set_threshold(6_500_000, MAIN_ADMIN_ID)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/config")
    await admin_cmds.cmd_config(update, ctx)
    said = update.last
    assert "PERSISTENT CONFIGURATION" in said
    assert "BTC HYPE" in said
    assert "6,500,000" in said
    assert "matches the database" in said


async def test_config_reports_drift_between_the_cache_and_the_database(container, ctx):
    """§18: the cache is not the source of truth, so /config must be able to say so."""
    async with container.db.session() as session:
        await SettingsRepository.set(session, KEY_MIN_WHALE, "12345678.0")
    update = FakeUpdate(MAIN_ADMIN_ID, text="/config")
    await admin_cmds.cmd_config(update, ctx)
    assert "Cache differs from the database" in update.last
    assert KEY_MIN_WHALE in update.last


async def test_config_never_prints_a_credential(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, text="/config")
    await admin_cmds.cmd_config(update, ctx)
    said = update.last
    for secret in (container.env.bot_token, container.env.database_url, "sqlite+aiosqlite"):
        assert secret not in said


async def test_config_is_refused_for_a_stranger(container, ctx):
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    update = FakeUpdate(STRANGER_ID, text="/config")
    await admin_cmds.cmd_config(update, ctx)
    assert "PERSISTENT CONFIGURATION" not in " ".join(update.sent)


# ── 7. the startup summary ─────────────────────────────────────

async def test_the_startup_summary_is_safe_and_informative(container):
    """§26: everything an operator needs, no secret."""
    await container.settings.set_coins(["BTC", "HYPE"], MAIN_ADMIN_ID)
    summary = container.startup_summary()
    assert summary["configuration"] in ("loaded", "seeded")
    assert summary["coins"] == "BTC, HYPE"
    assert summary["admins"] >= 1
    rendered = repr(summary)
    assert container.env.bot_token not in rendered
    assert container.env.database_url not in rendered
    assert "sqlite" in summary["database"]
    assert summary["persistence"] == "ephemeral"


async def test_the_summary_calls_postgres_durable(container, monkeypatch):
    """The signal an operator reads after a redeploy."""
    monkeypatch.setattr(type(container.db), "is_sqlite", property(lambda self: False))
    summary = container.startup_summary()
    assert summary["persistence"] == "durable"
    assert summary["database"] == "postgresql"
