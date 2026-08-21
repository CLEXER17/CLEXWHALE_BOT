"""Telegram commands, the inline-button router and the prompt flow.

Spec §36: "Telegram callback query handling", "Invalid command handling",
"Unauthorized user access". Spec §30 is the important one:

    "A user must never be able to trigger an admin callback by manually crafting
    callback data. Always verify the Telegram user ID against the authorized role
    before executing the callback."

So the tests here send hand-written payloads such as ``adm:remove_id:<id>`` from
a co-admin and from a stranger, and assert both that the refusal reaches them and
that the underlying state did not move.
"""

from __future__ import annotations

import pytest
from telegram import Update

from app.bot.handlers import admin as admin_cmds
from app.bot.handlers import common, prompts
from app.bot.handlers.callbacks import _capability_for, on_callback
from app.bot.keyboards import inline
from app.services.admin_service import ROLE_CO, ROLE_USER, Capability
from tests.conftest import (
    CO_ADMIN_ID,
    MAIN_ADMIN_ID,
    STRANGER_ID,
    FakeContext,
    FakeUpdate,
)


async def promote(container, telegram_id: int = CO_ADMIN_ID) -> None:
    await container.admins.add_co_admin(MAIN_ADMIN_ID, telegram_id, username="helper")


# ── §30: crafted callback data ─────────────────────────────────

async def test_a_stranger_cannot_trigger_an_admin_callback(container, ctx):
    """The attack: type the payload by hand instead of pressing the button."""
    await promote(container)
    update = FakeUpdate(STRANGER_ID, callback_data=f"adm:remove_id:{CO_ADMIN_ID}")
    await on_callback(update, ctx)

    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_CO   # nothing happened
    assert update.callback_query.edits == []                  # no panel was rendered
    assert update.callback_query.alerts                       # a refusal pop-up did appear
    assert "currently private" in update.callback_query.alerts[0]


async def test_a_co_admin_cannot_trigger_the_admin_removal_callback(container, ctx):
    """§28/§30: MANAGE_ADMINS is main-admin-only through every route."""
    await promote(container, CO_ADMIN_ID)
    await promote(container, 800_000_077)
    update = FakeUpdate(CO_ADMIN_ID, callback_data="adm:remove_id:800000077")
    await on_callback(update, ctx)

    assert container.admins.role_of(800_000_077) == ROLE_CO
    assert "Only the Main Admin" in update.callback_query.alerts[0]


async def test_a_co_admin_cannot_open_the_add_admin_prompt(container, ctx):
    await promote(container)
    update = FakeUpdate(CO_ADMIN_ID, callback_data="adm:add")
    await on_callback(update, ctx)
    assert "pending" not in ctx.user_data
    assert update.callback_query.alerts


async def test_a_co_admin_may_still_view_the_admin_list(container, ctx):
    """Reading the roster is a co-admin capability; changing it is not."""
    await promote(container)
    update = FakeUpdate(CO_ADMIN_ID, callback_data="adm:open")
    await on_callback(update, ctx)
    assert update.callback_query.edits
    assert update.callback_query.alerts == []


async def test_the_main_admin_can_remove_a_co_admin_by_callback(container, ctx):
    await promote(container)
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data=f"adm:remove_id:{CO_ADMIN_ID}")
    await on_callback(update, ctx)

    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_USER
    assert update.callback_query.edits
    # The demoted user is told, on their own chat id.
    assert ctx.bot.messages[0]["chat_id"] == CO_ADMIN_ID


async def test_a_stranger_cannot_change_the_threshold_by_callback(container, ctx):
    before = container.settings.config.min_whale_value
    update = FakeUpdate(STRANGER_ID, callback_data="thr:set:9000000")
    await on_callback(update, ctx)
    assert container.settings.config.min_whale_value == before
    assert update.callback_query.alerts


async def test_a_stranger_cannot_stop_monitoring_by_callback(container, ctx):
    await container.settings.set_monitoring(True, MAIN_ADMIN_ID)
    update = FakeUpdate(STRANGER_ID, callback_data="mon:stop")
    await on_callback(update, ctx)
    assert container.settings.config.monitoring_enabled is True
    assert update.callback_query.alerts


async def test_public_mode_does_not_open_the_control_callbacks(container, ctx):
    """§26: public mode grants *viewing*, never control."""
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    before = container.settings.config.min_whale_value

    controls = FakeUpdate(STRANGER_ID, callback_data="thr:set:9000000")
    await on_callback(controls, ctx)
    assert container.settings.config.min_whale_value == before
    assert controls.callback_query.alerts == ["🚫 You are not authorized to use this control."]

    view = FakeUpdate(STRANGER_ID, callback_data="data:whales")
    await on_callback(view, ctx)
    assert view.callback_query.edits


async def test_every_callback_area_maps_to_a_capability():
    """A new button cannot ship without a permission check."""
    areas = [
        inline.CB_PANEL,
        inline.CB_MON,
        inline.CB_THRESH,
        inline.CB_COIN,
        inline.CB_ADMIN,
        inline.CB_PUBLIC,
        inline.CB_SET,
        inline.CB_STATS,
        inline.CB_DATA,
    ]
    for area in areas:
        assert isinstance(_capability_for(area, "open"), Capability)
    # An unmapped area is treated as administrative, not public.
    assert _capability_for("something_new", "open") is Capability.CHANGE_SETTINGS
    assert _capability_for(inline.CB_ADMIN, "remove_id") is Capability.MANAGE_ADMINS
    assert _capability_for(inline.CB_ADMIN, "open") is Capability.VIEW_ADMINS


async def test_an_unknown_callback_area_changes_nothing(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="not_a_real_area:do_it")
    await on_callback(update, ctx)
    assert "no longer available" in update.last


async def test_a_noop_callback_is_acknowledged_silently(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data=inline.CB_NOOP)
    await on_callback(update, ctx)
    assert update.callback_query.answers == [{"text": None, "show_alert": False}]
    assert update.callback_query.edits == []


async def test_malformed_callback_data_does_not_raise(container, ctx):
    for payload in ("", ":", "adm", "adm:", "adm:remove_id:", "thr:set:not-a-number"):
        update = FakeUpdate(MAIN_ADMIN_ID, callback_data=payload)
        await on_callback(update, ctx)   # must not raise


async def test_callbacks_are_throttled(container, ctx):
    for _ in range(11):
        update = FakeUpdate(MAIN_ADMIN_ID, callback_data="panel:open")
        await on_callback(update, ctx)
    final = FakeUpdate(MAIN_ADMIN_ID, callback_data="panel:open")
    await on_callback(final, ctx)
    assert any("Too many" in text for text in final.sent)


# ── working callbacks ──────────────────────────────────────────

async def test_monitoring_toggle_flips_and_persists(container, ctx):
    await container.settings.set_monitoring(False, MAIN_ADMIN_ID)
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="mon:toggle")
    await on_callback(update, ctx)
    assert container.settings.config.monitoring_enabled is True

    from app.database.repository import SettingsRepository
    from app.services.settings_service import KEY_MONITORING

    async with container.db.session() as session:
        stored = await SettingsRepository.get(session, KEY_MONITORING)
    assert stored == "true"


async def test_threshold_button_applies_a_preset(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="thr:set:5000000")
    await on_callback(update, ctx)
    assert container.settings.config.min_whale_value == 5_000_000.0
    assert any("Threshold" in (a["text"] or "") for a in update.callback_query.answers)


async def test_coin_toggle_adds_then_removes(container, ctx):
    await container.settings.set_coins(["BTC"], MAIN_ADMIN_ID)
    await on_callback(FakeUpdate(MAIN_ADMIN_ID, callback_data="coin:toggle:ETH"), ctx)
    assert "ETH" in container.settings.config.coins
    await on_callback(FakeUpdate(MAIN_ADMIN_ID, callback_data="coin:toggle:ETH"), ctx)
    assert "ETH" not in container.settings.config.coins


async def test_public_mode_button_flips_the_mode(container, ctx):
    await on_callback(FakeUpdate(MAIN_ADMIN_ID, callback_data="pub:set:1"), ctx)
    assert container.settings.config.public_mode is True
    await on_callback(FakeUpdate(MAIN_ADMIN_ID, callback_data="pub:set:0"), ctx)
    assert container.settings.config.public_mode is False


async def test_only_whitelisted_switches_can_be_toggled(container, ctx):
    """`set:toggle:<key>` must not become a generic write into the settings store."""
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="set:toggle:public_mode")
    await on_callback(update, ctx)
    assert container.settings.config.public_mode is False
    assert "not available here" in update.callback_query.alerts[0]

    ok = FakeUpdate(MAIN_ADMIN_ID, callback_data="set:toggle:enable_order_detector")
    await on_callback(ok, ctx)
    assert container.settings.config.enable_order_detector is False


async def test_cancel_clears_a_pending_prompt(container, ctx):
    ctx.user_data["pending"] = {"kind": "threshold"}
    await on_callback(FakeUpdate(MAIN_ADMIN_ID, callback_data="panel:cancel"), ctx)
    assert "pending" not in ctx.user_data


# ── commands ───────────────────────────────────────────────────

async def test_start_tells_a_stranger_the_bot_is_private(container, ctx):
    update = FakeUpdate(STRANGER_ID, text="/start")
    await common.cmd_start(update, ctx)
    assert "currently private" in update.last


async def test_start_still_records_the_stranger_for_later(container, ctx):
    """§26: flipping public mode on must be able to reach people who tried."""
    from app.database.repository import UserRepository

    await common.cmd_start(FakeUpdate(STRANGER_ID, text="/start", chat_id=555), ctx)
    async with container.db.session() as session:
        row = await UserRepository.get(session, STRANGER_ID)
    assert row is not None and row.chat_id == 555


async def test_start_greets_the_main_admin_with_the_panel(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, text="/start")
    await common.cmd_start(update, ctx)
    # The panel reports the access mode ("private"/"public"); what it must not do
    # is show the stranger's refusal notice.
    assert "available only to authorized administrators" not in update.last
    assert "WHALE" in update.last.upper()
    assert "Main Admin" in update.last


async def test_help_is_private_for_a_stranger_and_open_in_public_mode(container, ctx):
    first = FakeUpdate(STRANGER_ID, text="/help")
    await common.cmd_help(first, ctx)
    assert "currently private" in first.last

    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    second = FakeUpdate(STRANGER_ID, text="/help")
    await common.cmd_help(second, ctx)
    assert "currently private" not in second.last


async def test_unknown_command_is_reported_to_an_admin(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, text="/definitelynotacommand")
    await common.cmd_unknown(update, ctx)
    assert "Unknown command" in update.last


async def test_unknown_command_does_not_leak_the_command_list(container, ctx):
    update = FakeUpdate(STRANGER_ID, text="/definitelynotacommand")
    await common.cmd_unknown(update, ctx)
    assert "currently private" in update.last
    assert "Unknown command" not in update.last


async def test_setthreshold_accepts_shorthand(container, ctx):
    ctx.args = ["5m"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 5m")
    await admin_cmds.cmd_setthreshold(update, ctx)
    assert container.settings.config.min_whale_value == 5_000_000.0


async def test_setthreshold_rejects_nonsense_without_changing_anything(container, ctx):
    before = container.settings.config.min_whale_value
    ctx.args = ["banana"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold banana")
    await admin_cmds.cmd_setthreshold(update, ctx)
    assert container.settings.config.min_whale_value == before
    assert "not a valid number" in update.last


async def test_setthreshold_clamps_and_says_so(container, ctx):
    ctx.args = ["1"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 1")
    await admin_cmds.cmd_setthreshold(update, ctx)
    assert container.settings.config.min_whale_value == 1_000.0
    assert "Adjusted to the allowed range" in update.last


async def test_a_stranger_cannot_run_setthreshold(container, ctx):
    before = container.settings.config.min_whale_value
    ctx.args = ["1000000000"]
    update = FakeUpdate(STRANGER_ID, text="/setthreshold 1000000000")
    await admin_cmds.cmd_setthreshold(update, ctx)
    assert container.settings.config.min_whale_value == before
    assert "currently private" in update.last


async def test_setmargin_accepts_shorthand(container, ctx):
    ctx.args = ["2m"]
    await admin_cmds.cmd_setmargin(FakeUpdate(MAIN_ADMIN_ID, text="/setmargin 2m"), ctx)
    assert container.settings.config.min_margin_value == 2_000_000.0
    assert container.settings.config.margin_gate_enabled is True


async def test_setmargin_zero_turns_the_gate_off(container, ctx):
    """``0`` is a valid margin (it disables the gate), unlike a $0 threshold."""
    ctx.args = ["2m"]
    await admin_cmds.cmd_setmargin(FakeUpdate(MAIN_ADMIN_ID, text="/setmargin 2m"), ctx)
    ctx.args = ["0"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setmargin 0")
    await admin_cmds.cmd_setmargin(update, ctx)
    assert container.settings.config.min_margin_value == 0.0
    assert container.settings.config.margin_gate_enabled is False
    assert "off" in update.last


async def test_setmargin_leaves_the_whale_threshold_alone(container, ctx):
    """Margin is collateral at risk; the threshold is notional. Never the same knob."""
    before = container.settings.config.min_whale_value
    ctx.args = ["2m"]
    await admin_cmds.cmd_setmargin(FakeUpdate(MAIN_ADMIN_ID, text="/setmargin 2m"), ctx)
    assert container.settings.config.min_whale_value == before


async def test_setmargin_rejects_nonsense_without_changing_anything(container, ctx):
    before = container.settings.config.min_margin_value
    ctx.args = ["banana"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setmargin banana")
    await admin_cmds.cmd_setmargin(update, ctx)
    assert container.settings.config.min_margin_value == before
    assert "not a valid number" in update.last


async def test_a_stranger_cannot_run_setmargin(container, ctx):
    before = container.settings.config.min_margin_value
    ctx.args = ["500000000"]
    update = FakeUpdate(STRANGER_ID, text="/setmargin 500000000")
    await admin_cmds.cmd_setmargin(update, ctx)
    assert container.settings.config.min_margin_value == before
    assert "currently private" in update.last


async def test_setcoins_replaces_the_list_and_leaves_all_coins_mode(container, ctx):
    await container.settings.set_value("all_coins", True, MAIN_ADMIN_ID)
    ctx.args = ["btc,", "eth"]
    await admin_cmds.cmd_setcoins(FakeUpdate(MAIN_ADMIN_ID, text="/setcoins btc, eth"), ctx)
    assert container.settings.config.coins == ("BTC", "ETH")
    assert container.settings.config.all_coins is False


async def test_setcoins_rejects_an_invalid_symbol(container, ctx):
    before = container.settings.config.coins
    ctx.args = ["BTC/USD"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/setcoins BTC/USD")
    await admin_cmds.cmd_setcoins(update, ctx)
    assert container.settings.config.coins == before
    assert "not a valid coin symbol" in update.last


async def test_addcoin_and_removecoin(container, ctx):
    await container.settings.set_coins(["BTC"], MAIN_ADMIN_ID)
    ctx.args = ["sol"]
    await admin_cmds.cmd_addcoin(FakeUpdate(MAIN_ADMIN_ID, text="/addcoin sol"), ctx)
    assert "SOL" in container.settings.config.coins

    ctx.args = ["SOL"]
    await admin_cmds.cmd_removecoin(FakeUpdate(MAIN_ADMIN_ID, text="/removecoin SOL"), ctx)
    assert "SOL" not in container.settings.config.coins


async def test_removecoin_of_an_absent_coin_is_reported_not_faked(container, ctx):
    await container.settings.set_coins(["BTC"], MAIN_ADMIN_ID)
    ctx.args = ["DOGE"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/removecoin DOGE")
    await admin_cmds.cmd_removecoin(update, ctx)
    assert "not in the list" in update.last


async def test_public_command_requires_on_or_off(container, ctx):
    ctx.args = ["maybe"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/public maybe")
    await admin_cmds.cmd_public(update, ctx)
    assert container.settings.config.public_mode is False
    assert "/public on" in update.last


async def test_startmonitor_and_stopmonitor(container, ctx):
    await admin_cmds.cmd_startmonitor(FakeUpdate(MAIN_ADMIN_ID, text="/startmonitor"), ctx)
    assert container.settings.config.monitoring_enabled is True
    await admin_cmds.cmd_stopmonitor(FakeUpdate(MAIN_ADMIN_ID, text="/stopmonitor"), ctx)
    assert container.settings.config.monitoring_enabled is False


async def test_a_co_admin_may_control_monitoring(container, ctx):
    """§29: monitoring control is shared; admin management is not."""
    await promote(container)
    await admin_cmds.cmd_startmonitor(FakeUpdate(CO_ADMIN_ID, text="/startmonitor"), ctx)
    assert container.settings.config.monitoring_enabled is True


async def test_addadmin_stores_the_numeric_id(container, ctx):
    ctx.args = [str(CO_ADMIN_ID)]
    update = FakeUpdate(MAIN_ADMIN_ID, text=f"/addadmin {CO_ADMIN_ID}")
    await admin_cmds.cmd_addadmin(update, ctx)
    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_CO
    assert str(CO_ADMIN_ID) in update.last


async def test_addadmin_refuses_a_username(container, ctx):
    """§32: a username can be changed, so it is never the stored identity."""
    ctx.args = ["@somebody"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/addadmin @somebody")
    await admin_cmds.cmd_addadmin(update, ctx)
    assert container.admins.co_admin_count == 0
    assert "not a Telegram user ID" in update.last
    assert "numeric ID is what gets stored" in update.last


async def test_a_co_admin_cannot_run_addadmin(container, ctx):
    await promote(container)
    ctx.args = [str(STRANGER_ID)]
    update = FakeUpdate(CO_ADMIN_ID, text=f"/addadmin {STRANGER_ID}")
    await admin_cmds.cmd_addadmin(update, ctx)
    assert container.admins.role_of(STRANGER_ID) == ROLE_USER
    assert "Only the Main Admin" in update.last


async def test_removeadmin_refuses_to_remove_the_main_admin(container, ctx):
    ctx.args = [str(MAIN_ADMIN_ID)]
    update = FakeUpdate(MAIN_ADMIN_ID, text=f"/removeadmin {MAIN_ADMIN_ID}")
    await admin_cmds.cmd_removeadmin(update, ctx)
    assert "Main Admin cannot be removed" in update.last


async def test_a_co_admin_cannot_read_the_audit_log(container, ctx):
    await promote(container)
    update = FakeUpdate(CO_ADMIN_ID, text="/audit")
    await admin_cmds.cmd_audit(update, ctx)
    assert "not authorized" in update.last or "Main Admin" in update.last


async def test_watch_rejects_a_non_address(container, ctx):
    ctx.args = ["definitely-not-an-address"]
    update = FakeUpdate(MAIN_ADMIN_ID, text="/watch definitely-not-an-address")
    await admin_cmds.cmd_watch(update, ctx)
    assert "not an EVM address" in update.last
    assert container.settings.config.tracked_wallets == ()


async def test_watch_stores_a_lowercased_address(container, ctx):
    address = "0x" + "AB" * 20
    ctx.args = [address]
    await admin_cmds.cmd_watch(FakeUpdate(MAIN_ADMIN_ID, text=f"/watch {address}"), ctx)
    assert address.lower() in container.settings.config.tracked_wallets


# ── parsing helpers ────────────────────────────────────────────

def test_parse_usd_accepts_the_documented_forms():
    assert admin_cmds.parse_usd("2000000") == 2_000_000.0
    assert admin_cmds.parse_usd("2,000,000") == 2_000_000.0
    assert admin_cmds.parse_usd("$2m") == 2_000_000.0
    assert admin_cmds.parse_usd("2M") == 2_000_000.0
    assert admin_cmds.parse_usd("1.5B") == 1_500_000_000.0
    assert admin_cmds.parse_usd("500k") == 500_000.0


def test_parse_usd_rejects_nonsense():
    for bad in ("", "banana", "2x", "-5", "0", "1e6", "$$2m", "2 000"):
        assert admin_cmds.parse_usd(bad) is None


def test_parse_coins_normalises_and_reports_invalid():
    valid, invalid = admin_cmds.parse_coins(["btc,", "eth", "SOL", "btc"])
    assert valid == ["BTC", "ETH", "SOL"]        # de-duplicated, uppercased
    assert invalid == []
    valid, invalid = admin_cmds.parse_coins(["BTC-PERP"])
    assert valid == [] and invalid == ["BTC-PERP"]


def test_parse_on_off():
    for text in ("on", "ON", "true", "1", "yes", "enable"):
        assert admin_cmds.parse_on_off(text) is True
    for text in ("off", "false", "0", "no", "disable"):
        assert admin_cmds.parse_on_off(text) is False
    assert admin_cmds.parse_on_off("maybe") is None


def test_parse_user_id_rejects_usernames_and_junk():
    assert admin_cmds.parse_user_id("12345") == 12345
    assert admin_cmds.parse_user_id(" 12345 ") == 12345
    for bad in ("@handle", "abc", "", "-1", "0", "12.5"):
        assert admin_cmds.parse_user_id(bad) is None


# ── the prompt flow ────────────────────────────────────────────

async def test_a_prompt_answer_is_applied(container, ctx):
    ctx.user_data["pending"] = {"kind": "threshold"}
    update = FakeUpdate(MAIN_ADMIN_ID, text="7,500,000")
    await prompts.on_text(update, ctx)
    assert container.settings.config.min_whale_value == 7_500_000.0
    assert "pending" not in ctx.user_data


async def test_a_prompt_is_consumed_even_when_the_answer_is_invalid(container, ctx):
    """The next message must not be silently reinterpreted as another answer."""
    ctx.user_data["pending"] = {"kind": "threshold"}
    update = FakeUpdate(MAIN_ADMIN_ID, text="banana")
    await prompts.on_text(update, ctx)
    assert "pending" not in ctx.user_data
    assert "not a valid number" in update.last


async def test_a_prompt_can_be_cancelled_by_typing_cancel(container, ctx):
    ctx.user_data["pending"] = {"kind": "coins"}
    update = FakeUpdate(MAIN_ADMIN_ID, text="cancel")
    await prompts.on_text(update, ctx)
    assert "pending" not in ctx.user_data
    assert "Cancelled" in update.last


async def test_capability_is_rechecked_when_the_answer_arrives(container, ctx):
    """§30 again: a prompt is not a stored grant of authority."""
    await promote(container)
    ctx.user_data["pending"] = {"kind": "admin_add"}
    # The co-admin was never entitled to this prompt; answering it changes nothing.
    update = FakeUpdate(CO_ADMIN_ID, text=str(STRANGER_ID))
    await prompts.on_text(update, ctx)
    assert container.admins.role_of(STRANGER_ID) == ROLE_USER
    assert "pending" not in ctx.user_data
    assert "Only the Main Admin" in update.last


async def test_a_demotion_between_prompt_and_answer_is_honoured(container, ctx):
    await promote(container)
    ctx.user_data["pending"] = {"kind": "threshold"}
    before = container.settings.config.min_whale_value
    await container.admins.remove_co_admin(MAIN_ADMIN_ID, CO_ADMIN_ID)

    update = FakeUpdate(CO_ADMIN_ID, text="9000000")
    await prompts.on_text(update, ctx)
    assert container.settings.config.min_whale_value == before
    assert "currently private" in update.last


async def test_an_unknown_pending_kind_is_dropped(container, ctx):
    ctx.user_data["pending"] = {"kind": "something_removed_in_a_refactor"}
    update = FakeUpdate(MAIN_ADMIN_ID, text="whatever")
    await prompts.on_text(update, ctx)
    assert "pending" not in ctx.user_data
    assert update.sent == []


async def test_plain_text_from_a_stranger_reveals_nothing(container, ctx):
    update = FakeUpdate(STRANGER_ID, text="hello?")
    await prompts.on_text(update, ctx)
    assert "currently private" in update.last


async def test_the_admin_add_prompt_answer_promotes(container, ctx):
    ctx.user_data["pending"] = {"kind": "admin_add"}
    update = FakeUpdate(MAIN_ADMIN_ID, text=str(CO_ADMIN_ID))
    await prompts.on_text(update, ctx)
    assert container.admins.role_of(CO_ADMIN_ID) == ROLE_CO
    assert ctx.bot.messages[0]["chat_id"] == CO_ADMIN_ID


async def test_the_wallet_prompt_validates_the_address(container, ctx):
    ctx.user_data["pending"] = {"kind": "wallet"}
    update = FakeUpdate(MAIN_ADMIN_ID, text="0xnope")
    await prompts.on_text(update, ctx)
    assert "not an EVM address" in update.last
    assert container.settings.config.tracked_wallets == ()


# ── the error handler ──────────────────────────────────────────
#
# ``on_error`` deliberately checks ``isinstance(update, Update)`` before touching
# anything, so these tests build *real* PTB objects (with the fake bot attached)
# rather than the duck-typed stand-in used everywhere else.

def real_update(bot, *, text: str | None = None, callback_data: str | None = None) -> Update:
    user = {"id": MAIN_ADMIN_ID, "is_bot": False, "first_name": "Test"}
    chat = {"id": MAIN_ADMIN_ID, "type": "private"}
    message = {"message_id": 1, "date": 1_700_000_000, "chat": chat, "from": user, "text": text}
    if callback_data is not None:
        payload = {
            "update_id": 1,
            "callback_query": {
                "id": "cbq-1",
                "from": user,
                "chat_instance": "ci-1",
                "message": message,
                "data": callback_data,
            },
        }
    else:
        payload = {"update_id": 1, "message": message}
    built = Update.de_json(payload, bot)
    assert built is not None
    return built


async def test_the_error_handler_answers_a_broken_callback(container, ctx):
    update = real_update(ctx.bot, callback_data="panel:open")
    ctx.error = RuntimeError("boom")
    await common.on_error(update, ctx)
    assert ctx.bot.answers == [
        {"text": "Something went wrong. Please try again.", "show_alert": True}
    ]


async def test_the_error_handler_messages_a_broken_command(container, ctx):
    update = real_update(ctx.bot, text="/status")
    ctx.error = RuntimeError("boom")
    await common.on_error(update, ctx)
    assert "Something went wrong" in ctx.bot.messages[0]["text"]
    assert ctx.bot.messages[0]["chat_id"] == MAIN_ADMIN_ID


async def test_the_error_handler_ignores_a_non_update(container, ctx):
    """PTB also passes strings and dicts here; that must not raise."""
    ctx.error = RuntimeError("boom")
    await common.on_error("not an update", ctx)
    await common.on_error({"update_id": 1}, ctx)
    assert ctx.bot.messages == []


async def test_the_error_handler_survives_a_failing_send(container, ctx):
    """If Telegram is the thing that broke, the handler must not recurse."""
    update = real_update(ctx.bot, text="/status")
    ctx.bot.raises = RuntimeError("telegram is down")
    ctx.error = RuntimeError("boom")
    await common.on_error(update, ctx)   # must not raise
    assert ctx.bot.messages == []
