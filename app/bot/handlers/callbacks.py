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
    inline.CB_STATS: Capability.VIEW_STATS,
    inline.CB_DATA: Capability.VIEW_WHALES,
}

#: Read-only exceptions: viewing the admin list needs VIEW_ADMINS, which a
#: co-admin has; mutating it needs MANAGE_ADMINS, which only the main admin has.
_ACTION_CAPABILITY = {
    (inline.CB_ADMIN, "open"): Capability.VIEW_ADMINS,
    (inline.CB_ADMIN, "list"): Capability.VIEW_ADMINS,
}

#: Booleans the 🔔 Alert Settings panel may flip. Monitoring and public mode are
#: deliberately absent — they have their own capabilities and their own panels.
_ALERT_TOGGLES = frozenset(
    {KEY_TRADES, KEY_POSITIONS, KEY_ORDERS, KEY_CANCELS, KEY_WALLETS, KEY_BOOK}
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
        await settings.set_coins([], actor.telegram_id)
        text, keyboard = await views.coins_view(container, actor)
        await respond(update, text, keyboard, toast="Coin list cleared")
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

    text, keyboard = await views.settings_view(container)
    await respond(update, text, keyboard)


async def _tell(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, message: str) -> None:
    try:
        await context.bot.send_message(telegram_id, message, parse_mode="HTML")
    except Exception as exc:
        log.info(
            "Could not notify the affected administrator",
            extra={"telegram_id": telegram_id, "reason": type(exc).__name__},
        )
