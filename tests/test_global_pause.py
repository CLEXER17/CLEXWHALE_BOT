"""The global pause: ``/pause`` stops everything, ``/go`` starts it again.

    "also add a global pause command so the bot stop entierly from everything
    then to make it run we need to send go command"

"Entirely" is the whole point, and it is what makes this worth its own file. The
pause is not a second monitoring switch: ``/stopmonitor`` turns the detectors
off and leaves the bot answering commands, whereas a pause has to reach the
market feeds, the detection pipeline, alert delivery, every command, every inline
button and every half-finished prompt.

Four properties are easy to get wrong, so each has a test here:

* **Only ``/go`` gets through.** Enforced once, in the middleware, rather than in
  each handler — a per-handler check is a check somebody forgets to add.
* **The exemptions are deliberate and minimal.** ``/go`` lifts the pause;
  ``/status`` and ``/panel`` are the read-only views that explain why everything
  else is refused, so refusing them would leave an admin with no way to find out.
  ``/stop`` is never refused either: being asked to stop messaging someone is not
  a privileged operation, and a paused bot must still honour it.
* **``monitoring_enabled`` is not touched.** ``/go`` restores what was
  configured before the pause instead of turning monitoring on.
* **The pause is durable.** It is a database setting, so a redeploy comes back
  paused rather than silently resuming.
"""

from __future__ import annotations

from app.bot.handlers import admin as admin_cmds
from app.bot.handlers import common, data, prompts
from app.bot.handlers.callbacks import on_callback
from app.container import AppContainer
from app.utils.formatting import DataPoint
from app.whale.events import EventType, ValueKind, WhaleEvent
from tests.conftest import MAIN_ADMIN_ID, STRANGER_ID, FakeBot, FakeUpdate
from tests.factories import WALLET_A

PAUSED_NOTICE = "The bot is paused"


async def pause(container) -> None:
    await container.settings.set_paused(True, MAIN_ADMIN_ID)


def alertable_event() -> WhaleEvent:
    event = WhaleEvent(
        event_type=EventType.WHALE_TRADE,
        coin="BTC",
        notional=9_000_000.0,
        value_kind=ValueKind.TRADE_VALUE,
        side="BUY",
        wallet=WALLET_A,
        detection="Large market trade",
        dedup_key="pause-test",
    )
    event.set("price", DataPoint.confirmed(100_000.0))
    event.set("size", DataPoint.confirmed(90.0))
    return event


# ── the pause reaches every entry point ────────────────────────

async def test_pause_refuses_every_other_command(container, ctx):
    """One middleware gate, so no handler can forget it."""
    update = FakeUpdate(MAIN_ADMIN_ID, text="/pause")
    await admin_cmds.cmd_pause(update, ctx)
    assert container.settings.config.paused is True
    assert "BOT PAUSED" in update.last

    before = container.settings.config.min_whale_value
    ctx.args = ["9000000"]
    refused = FakeUpdate(MAIN_ADMIN_ID, text="/setthreshold 9000000")
    await admin_cmds.cmd_setthreshold(refused, ctx)

    assert container.settings.config.min_whale_value == before
    assert PAUSED_NOTICE in refused.last
    assert "/go" in refused.last          # an admin is told how to lift it


async def test_pause_refuses_every_inline_button_except_resume(container, ctx):
    await pause(container)

    for payload in ("thresh:open", "coin:open", "set:alerts", "adm:open", "mon:stop"):
        update = FakeUpdate(MAIN_ADMIN_ID, callback_data=payload)
        await on_callback(update, ctx)
        assert update.callback_query.edits == [], f"{payload} acted behind the pause"
        assert any(PAUSED_NOTICE in alert for alert in update.callback_query.alerts)

    assert container.settings.config.paused is True


async def test_the_resume_button_lifts_the_pause(container, ctx):
    """The panel renders ▶️ RESUME BOT while paused; it has to work."""
    await pause(container)
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="mon:resume")
    await on_callback(update, ctx)

    assert container.settings.config.paused is False
    assert update.callback_query.edits
    assert any(
        "Bot resumed" in (answer["text"] or "") for answer in update.callback_query.answers
    )


async def test_go_lifts_the_pause_and_shows_the_restored_state(container, ctx):
    await pause(container)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/go")
    await admin_cmds.cmd_go(update, ctx)

    assert container.settings.config.paused is False
    assert any("BOT RESUMED" in text for text in update.sent)
    assert any("MONITORING STATUS" in text for text in update.sent)


async def test_a_pending_prompt_is_dropped_when_a_pause_lands_mid_flow(container, ctx):
    """A prompt outlives the command that opened it.

    The panel asks "send the new threshold"; the pause arrives before the answer
    does. Applying the change afterwards would edit configuration while
    everything else is frozen, so the prompt is discarded instead.
    """
    ctx.user_data["pending"] = {"kind": "threshold"}
    before = container.settings.config.min_whale_value
    await pause(container)

    update = FakeUpdate(MAIN_ADMIN_ID, text="9000000")
    await prompts.on_text(update, ctx)

    assert container.settings.config.min_whale_value == before
    assert PAUSED_NOTICE in update.last
    assert "pending" not in ctx.user_data


# ── the deliberate exemptions ──────────────────────────────────

async def test_status_still_explains_the_pause(container, ctx):
    """Refusing /status would leave an admin unable to see *why* it is refused."""
    await pause(container)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/status")
    await data.cmd_status(update, ctx)

    assert "GLOBALLY PAUSED" in update.last
    assert "/go" in update.last
    assert PAUSED_NOTICE not in update.last


async def test_the_panel_still_opens_and_offers_resume(container, ctx):
    """The pause is liftable from the UI, not only by typing /go."""
    await pause(container)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/panel")
    await common.cmd_panel(update, ctx)

    assert PAUSED_NOTICE not in update.last
    markup = update.effective_chat.sent[-1].get("reply_markup")
    assert markup is not None
    payloads = [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]
    assert "mon:resume" in payloads


async def test_stop_is_never_refused_even_while_paused(container, ctx, database):
    """Being asked to stop messaging someone is not a privileged operation."""
    await pause(container)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/stop")
    await common.cmd_stop(update, ctx)

    assert PAUSED_NOTICE not in update.last
    from app.database.repository import UserRepository

    async with database.session() as session:
        assert MAIN_ADMIN_ID in await UserRepository.unsubscribed_ids(session)


async def test_a_normal_user_is_not_told_how_to_resume(container, ctx):
    """Issue 5: naming /go would advertise a privileged command."""
    await container.settings.set_public_mode(True, MAIN_ADMIN_ID)
    await pause(container)

    update = FakeUpdate(STRANGER_ID, text="/whales")
    await data.cmd_whales(update, ctx)

    assert PAUSED_NOTICE in update.last
    assert "/go" not in update.last
    assert "administrator has stopped it" in update.last


# ── what the pause must not change ─────────────────────────────

async def test_pause_leaves_the_monitoring_switch_alone(container):
    """``/go`` restores what was configured, it does not turn monitoring on."""
    await container.settings.set_monitoring(False, MAIN_ADMIN_ID)
    await pause(container)
    assert container.settings.config.monitoring_enabled is False
    assert container.settings.config.monitoring_active is False

    await container.settings.set_paused(False, MAIN_ADMIN_ID)
    assert container.settings.config.monitoring_enabled is False   # still off, as configured

    await container.settings.set_monitoring(True, MAIN_ADMIN_ID)
    await pause(container)
    assert container.settings.config.monitoring_enabled is True
    assert container.settings.config.monitoring_active is False    # the pause still wins


async def test_nothing_is_delivered_while_paused(container):
    bot = FakeBot()
    container.alerts.attach_bot(bot)
    await container.alerts.start()
    await pause(container)

    await container.alerts.enqueue(alertable_event())

    assert bot.messages == []
    stats = container.alerts.stats()
    assert stats["paused_drops"] == 1
    assert stats["queued"] == 0
    assert any("Globally paused" in warning for warning in container.runtime_warnings())


async def test_the_pause_survives_a_redeploy(env, database, container):
    """It is a database setting, not process state.

    A paused bot that came back running after a Railway redeploy would resume
    alerting without anyone asking it to.
    """
    await pause(container)

    replacement = AppContainer(env, database)
    await replacement.restore()
    assert replacement.settings.config.paused is True

    await replacement.settings.set_paused(False, MAIN_ADMIN_ID)
    third = AppContainer(env, database)
    await third.restore()
    assert third.settings.config.paused is False


# ── idempotence ────────────────────────────────────────────────

async def test_pausing_twice_is_not_an_error(container, ctx):
    await pause(container)
    update = FakeUpdate(MAIN_ADMIN_ID, text="/pause")
    await admin_cmds.cmd_pause(update, ctx)
    assert container.settings.config.paused is True
    assert "BOT PAUSED" in update.last


async def test_go_when_nothing_is_paused_says_so(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, text="/go")
    await admin_cmds.cmd_go(update, ctx)

    assert container.settings.config.paused is False
    assert any("Already running" in text for text in update.sent)


async def test_the_resume_button_reports_no_change_when_already_running(container, ctx):
    update = FakeUpdate(MAIN_ADMIN_ID, callback_data="mon:resume")
    await on_callback(update, ctx)

    assert container.settings.config.paused is False
    assert any("No change" in (answer["text"] or "") for answer in update.callback_query.answers)
