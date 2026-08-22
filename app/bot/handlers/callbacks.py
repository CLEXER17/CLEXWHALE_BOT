"""The inline-button router.

Callback data is untrusted. This module is the *only* place buttons are turned
into actions, and it re-derives the caller's role from
``update.effective_user.id`` before dispatching — never from the callback payload
(spec §30). Sending ``adm:remove_id:123`` by hand therefore lands in exactly the
same authorization check as pressing the button, and a co-admin or ordinary user
is refused.

The capability required for a callback is looked up from the area/action pair, so
a new button cannot accidentally ship without a permission check: an unknown area
falls through to :data:`Capability.CHANGE_SETTINGS`, the admin-only default.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.bot import views
from app.bot.commands import sync_command_menus
from app.bot.handlers import admin as admin_cmds
from app.bot.keyboards import inline
from app.bot.messages import texts
from app.bot.middleware.permissions import (
    get_container,
    notify,
    refuse,
    register,
    respond,
    throttle,
)
from app.container import AppContainer
from app.services.admin_service import Actor, AdminError, Capability
from app.services.settings_service import (
    KEY_ALL_COINS,
    KEY_BOOK,
    KEY_CANCELS,
    KEY_ORDER_ALERTS,
    KEY_ORDERS,
    KEY_POSITIONS,
    KEY_TRADES,
    KEY_WALLETS,
)
from app.utils.logging import get_logger

log = get_logger(__name__)

#: Default capability per callback area. Anything unmapped is treated as an
#: administrative action rather than a public one.
_AREA_CAPABILITY = {
    inline.CB_PANEL: Capability.CHANGE_SETTINGS,
    inline.CB_MON: Capability.CONTROL_MONITORING,
    inline.CB_THRESH: Capability.CHANGE_THRESHOLD,
    inline.CB_MARGIN: Capability.CHANGE_THRESHOLD,
    inline.CB_COIN: Capability.CHANGE_COINS,
    inline.CB_ADMIN: Capability.MANAGE_ADMINS,
    inline.CB_PUBLIC: Capability.CHANGE_PUBLIC_MODE,
    inline.CB_SET: Capability.CHANGE_SETTINGS,
    inline.CB_RESET: Capability.RESET_SETTINGS,
    inline.CB_STATS: Capability.VIEW_STATS,
    inline.CB_DATA: Capability.VIEW_WHALES,
}

#: Read-only exceptions: viewing the admin list needs VIEW_ADMINS, which a
#: co-admin has; mutating it needs MANAGE_ADMINS, which only the main admin has.
_ACTION_CAPABILITY = {
    (inline.CB_ADMIN, "open"): Capability.VIEW_ADMINS,
    (inline.CB_ADMIN, "list"): Capability.VIEW_ADMINS,
}

#: The only buttons that still work while the bot is globally paused: the one
#: that lifts the pause, and the read-only status that explains it. Everything
#: else is refused in :func:`on_callback` so no panel can act behind the pause.
_PAUSE_EXEMPT = frozenset(
    {(inline.CB_MON, "resume"), (inline.CB_MON, "status"), (inline.CB_PANEL, "open")}
)

#: Booleans the 🔔 Alert Settings panel may flip. Monitoring and public mode are
#: deliberately absent — they have their own capabilities and their own panels.
#:
#: ``KEY_ORDER_ALERTS`` is the resting-order row (Task E: the panel decides what is
#: *sent*). ``KEY_ORDERS`` — internal order tracking — stays reachable here so the
#: ``/set`` command can still turn tracking off, but it is not what the panel's
#: order row flips: silencing order alerts must never disable the fill attribution
#: the trade feed is built on.
_ALERT_TOGGLES = frozenset(
    {
        KEY_TRADES,
        KEY_POSITIONS,
        KEY_ORDERS,
        KEY_ORDER_ALERTS,
        KEY_CANCELS,
        KEY_WALLETS,
        KEY_BOOK,
    }
)


def _capability_for(area: str, action: str) -> Capability:
    return _ACTION_CAPABILITY.get(
        (area, action), _AREA_CAPABILITY.get(area, Capability.CHANGE_SETTINGS)
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    area, action, arg = _parse(query.data or "")
    if area == inline.CB_NOOP:
        await query.answer()
        return

    if not throttle(user.id):
        await notify(update, texts.rate_limited(), alert=True)
        return

    container = get_container(context)
    capability = _capability_for(area, action)
    try:
        actor = container.admins.require(
            user.id, capability, public_mode=container.settings.config.public_mode
        )
    except AdminError as exc:
        log.warning(
            "Refused a callback",
            extra={
                "telegram_id": user.id,
                "role": container.admins.role_of(user.id),
                "callback": f"{area}:{action}",
            },
        )
        await refuse(update, str(exc))
        return

    # Checked after authorization, so an unauthorized caller still learns only
    # that it was refused — not what state the bot is in.
    if container.settings.config.paused and (area, action) not in _PAUSE_EXEMPT:
        await refuse(update, texts.paused_notice(admin=actor.is_admin))
        return

    await register(update, container)

    try:
        await _dispatch(update, context, container, actor, area, action, arg)
    except AdminError as exc:
        await refuse(update, str(exc))


def _parse(data: str) -> tuple[str, str, str | None]:
    parts = data.split(":")
    area = parts[0] if parts else ""
    action = parts[1] if len(parts) > 1 else "open"
    arg = parts[2] if len(parts) > 2 else None
    return area, action, arg


async def _dispatch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    container: AppContainer,
    actor: Actor,
    area: str,
    action: str,
    arg: str | None,
) -> None:
    if area == inline.CB_PANEL:
        if action == "cancel":
            context.user_data.pop("pending", None)
            text, keyboard = await views.panel_view(container, actor)
            await respond(update, text, keyboard, toast=texts.CANCELLED)
            return
        text, keyboard = await views.panel_view(container, actor)
        await respond(update, text, keyboard)
        return

    if area == inline.CB_MON:
        await _monitoring(update, container, actor, action)
        return

    if area == inline.CB_THRESH:
        await _threshold(update, context, container, actor, action, arg)
        return

    if area == inline.CB_MARGIN:
        await _margin(update, context, container, actor, action, arg)
        return

    if area == inline.CB_COIN:
        await _coins(update, context, container, actor, action, arg)
        return

    if area == inline.CB_ADMIN:
        await _admins(update, context, container, actor, action, arg)
        return

    if area == inline.CB_PUBLIC:
        await _public(update, container, actor, action, arg)
        return

    if area == inline.CB_SET:
        await _settings(update, context, container, actor, action, arg)
        return

    if area == inline.CB_RESET:
        await _reset(update, container, actor, action)
        return

    if area == inline.CB_STATS:
        if action == "diag":
            text, keyboard = await views.diagnostics_view(container, actor)
        else:
            text, keyboard = await views.stats_view(container, actor)
        await respond(update, text, keyboard)
        return

    if area == inline.CB_DATA:
        text, keyboard = await views.data_view(container, actor, action)
        await respond(update, text, keyboard)
        return

    # Unknown area: acknowledge so the client stops spinning, change nothing.
    log.info("Ignored an unknown callback area", extra={"area": area})
    await notify(update, "That control is no longer available.")


# ── area handlers ──────────────────────────────────────────────
async def _monitoring(
    update: Update, container: AppContainer, actor: Actor, action: str
) -> None:
    config = container.settings.config
    if action in ("pause", "resume"):
        # The global pause is a different switch from `monitoring_enabled`: it is
        # left untouched here so resuming restores exactly what was configured.
        target = action == "pause"
        if config.paused == target:
            await respond(update, *await views.status_view(container), toast="No change")
            return
        await container.settings.set_paused(target, actor.telegram_id)
        text, keyboard = await views.status_view(container)
        toast = "Bot paused — /go to resume" if target else "Bot resumed"
        await respond(update, text, keyboard, toast=toast)
        return

    if action == "toggle":
        target = not config.monitoring_enabled
    elif action == "start":
        target = True
    elif action == "stop":
        target = False
    else:  # "status"
        text, keyboard = await views.status_view(container)
        await respond(update, text, keyboard)
        return

    await container.settings.set_monitoring(target, actor.telegram_id)
    text, keyboard = await views.status_view(container)
    toast = texts.monitoring_started() if target else texts.monitoring_stopped()
    await respond(update, text, keyboard, toast=toast.split("\n")[0])


async def _threshold(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    container: AppContainer,
    actor: Actor,
    action: str,
    arg: str | None,
) -> None:
    if action == "prompt":
        context.user_data["pending"] = {"kind": "threshold"}
        await respond(update, texts.PROMPT_THRESHOLD, inline.cancel_prompt())
        return

    if action == "set" and arg:
        requested = admin_cmds.parse_usd(arg)
        if requested is None:
            await notify(update, "That is not a valid amount.", alert=True)
            return
        applied = await container.settings.set_threshold(requested, actor.telegram_id)
        text, keyboard = await views.threshold_view(container)
        await respond(update, text, keyboard, toast=f"Threshold: ${applied:,.0f}")
        return

    text, keyboard = await views.threshold_view(container)
    await respond(update, text, keyboard)


async def _margin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    container: AppContainer,
    actor: Actor,
    action: str,
    arg: str | None,
) -> None:
    if action == "prompt":
        context.user_data["pending"] = {"kind": "margin"}
        await respond(update, texts.PROMPT_MARGIN, inline.cancel_prompt())
        return

    if action == "set" and arg is not None:
        # "0" is a legitimate value here — it turns the gate off — so it is
        # parsed rather than treated as a missing argument.
        requested = admin_cmds.parse_margin(arg)
        if requested is None:
            await notify(update, "That is not a valid amount.", alert=True)
            return
        applied = await container.settings.set_margin(requested, actor.telegram_id)
        text, keyboard = await views.margin_view(container)
        toast = "Margin gate off" if applied <= 0 else f"Margin: ${applied:,.0f}"
        await respond(update, text, keyboard, toast=toast)
        return

    text, keyboard = await views.margin_view(container)
    await respond(update, text, keyboard)


async def _coins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    container: AppContainer,
    actor: Actor,
    action: str,
    arg: str | None,
) -> None:
    settings = container.settings

    if action == "prompt":
        context.user_data["pending"] = {"kind": "coins"}
        await respond(update, texts.PROMPT_COINS, inline.cancel_prompt())
        return

    if action == "all" and arg is not None:
        await settings.set_value(KEY_ALL_COINS, arg == "1", actor.telegram_id)
        text, keyboard = await views.coins_view(container, actor)
        await respond(
            update, text, keyboard, toast="ALL COINS" if arg == "1" else "SELECTED COINS"
        )
        return

    if action == "toggle" and arg:
        coins, invalid = admin_cmds.parse_coins([arg])
        if invalid or not coins:
            await notify(update, "That is not a valid coin symbol.", alert=True)
            return
        coin = coins[0]
        if coin in settings.config.coins:
            await settings.remove_coin(coin, actor.telegram_id)
            toast = f"{coin} removed"
        else:
            await settings.add_coin(coin, actor.telegram_id)
            toast = f"{coin} added"
            universe = container.engine.known_coins
            if universe and coin not in universe:
                toast = f"{coin} added — not a Hyperliquid perp"
        text, keyboard = await views.coins_view(container, actor)
        await respond(update, text, keyboard, toast=toast)
        return

    if action == "clear":
        # Ask first. This is the only coin button that can remove everything, so
        # it shows what it is about to destroy and changes nothing yet (§22/§23).
        if not settings.config.coins:
            await notify(update, "The coin list is already empty.", alert=True)
            return
        await respond(
            update, texts.confirm_clear_coins(settings.config), inline.confirm_clear_coins()
        )
        return

    if action == "clearyes":
        removed = settings.config.coins
        await settings.set_coins([], actor.telegram_id)
        await respond(update, texts.coins_cleared(removed))
        text, keyboard = await views.coins_view(container, actor)
        await respond(update, text, keyboard, edit=False)
        return

    text, keyboard = await views.coins_view(container, actor)
    await respond(update, text, keyboard)


async def _admins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    container: AppContainer,
    actor: Actor,
    action: str,
    arg: str | None,
) -> None:
    if action == "add":
        context.user_data["pending"] = {"kind": "admin_add"}
        await respond(update, texts.PROMPT_ADD_ADMIN, inline.cancel_prompt())
        return

    if action == "list":
        # Its own panel, not a re-render of the admin home this button sits on:
        # identical content makes Telegram reject the edit ("message is not
        # modified") and the button looks broken.
        text, keyboard = await views.admin_roster_view(container, actor)
        await respond(update, text, keyboard, toast="Co-admin roster")
        return

    if action == "remove":
        text, keyboard = await views.admin_remove_view(container, actor)
        await respond(update, text, keyboard)
        return

    if action == "remove_id" and arg:
        target = admin_cmds.parse_user_id(arg)
        if target is None:
            await notify(update, "That is not a Telegram user ID.", alert=True)
            return
        # AdminError propagates to on_callback and is shown as an alert.
        await container.admins.remove_co_admin(actor.telegram_id, target)
        container.alerts.invalidate_recipients()
        text, keyboard = await views.admin_home_view(container, actor)
        await respond(update, text, keyboard, toast=f"Removed {target}")
        await sync_command_menus(context.bot, container, demoted=target)
        await _tell(context, target, texts.demoted_notice())
        return

    text, keyboard = await views.admin_home_view(container, actor)
    await respond(update, text, keyboard)


async def _public(
    update: Update,
    container: AppContainer,
    actor: Actor,
    action: str,
    arg: str | None,
) -> None:
    if action == "set" and arg is not None:
        await container.settings.set_public_mode(arg == "1", actor.telegram_id)
        container.alerts.invalidate_recipients()
        text, keyboard = await views.public_mode_view(container)
        await respond(update, text, keyboard, toast="PUBLIC" if arg == "1" else "PRIVATE")
        return
    text, keyboard = await views.public_mode_view(container)
    await respond(update, text, keyboard)


async def _settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    container: AppContainer,
    actor: Actor,
    action: str,
    arg: str | None,
) -> None:
    if action == "alerts":
        text, keyboard = await views.alert_settings_view(container)
        await respond(update, text, keyboard)
        return

    if action == "toggle" and arg:
        if arg not in _ALERT_TOGGLES:
            await notify(update, "That switch is not available here.", alert=True)
            return
        value = await container.settings.toggle(arg, actor.telegram_id)
        text, keyboard = await views.alert_settings_view(container)
        await respond(update, text, keyboard, toast="ON" if value else "OFF")
        return

    if action == "cooldown_open":
        text, keyboard = await views.cooldown_view(container)
        await respond(update, text, keyboard)
        return

    if action == "cooldown_prompt":
        context.user_data["pending"] = {"kind": "cooldown"}
        await respond(update, texts.PROMPT_COOLDOWN, inline.cancel_prompt())
        return

    if action == "cooldown" and arg is not None:
        try:
            seconds = int(arg)
        except ValueError:
            await notify(update, "That is not a number of seconds.", alert=True)
            return
        applied = await container.settings.set_cooldown(seconds, actor.telegram_id)
        text, keyboard = await views.cooldown_view(container)
        await respond(update, text, keyboard, toast=f"Cooldown {applied}s")
        return

    if action == "config":
        text, keyboard = await views.config_view(container)
        await respond(update, text, keyboard)
        return

    text, keyboard = await views.settings_view(container)
    await respond(update, text, keyboard)


async def _reset(
    update: Update, container: AppContainer, actor: Actor, action: str
) -> None:
    """The confirmed settings reset (spec §23).

    Reached only from :func:`app.bot.keyboards.inline.confirm_reset_settings`, and
    only by the main admin — the area's capability is ``RESET_SETTINGS``, which
    :meth:`AdminService.can` grants to no one else, so forging ``reset:confirm``
    gets the same refusal a co-admin would get from the button.
    """
    if action != "confirm":
        await respond(
            update,
            texts.confirm_reset_settings(container.settings.config),
            inline.confirm_reset_settings(),
        )
        return
    config = await container.settings.reset_to_defaults(actor.telegram_id)
    await respond(update, texts.settings_reset(config))
    text, keyboard = await views.settings_view(container)
    await respond(update, text, keyboard, edit=False)


async def _tell(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, message: str) -> None:
    try:
        await context.bot.send_message(telegram_id, message, parse_mode="HTML")
    except Exception as exc:
        log.info(
            "Could not notify the affected administrator",
            extra={"telegram_id": telegram_id, "reason": type(exc).__name__},
        )
