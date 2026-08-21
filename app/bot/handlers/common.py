"""Entry-point commands, the unknown-command fallback and the error handler."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.bot import views
from app.bot.messages import texts
from app.bot.middleware.permissions import (
    actor_of,
    get_container,
    register,
    requires,
    respond,
)
from app.container import AppContainer
from app.services.admin_service import Actor, Capability
from app.utils.logging import get_logger

log = get_logger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/start`` — the only command an unauthorized user may reach.

    Deliberately not wrapped in :func:`requires`: a private bot still has to be
    able to tell a stranger it is private. Contact details are recorded so that
    flipping public mode on later can reach people who already tried, but no
    whale data is disclosed.

    ``/start`` is also the way back from ``/stop``: it clears the opt-out, which
    is why it re-subscribes explicitly instead of relying on registration.
    """
    container = get_container(context)
    await register(update, container)
    resumed = await _set_alerts(update, container, True)
    actor = actor_of(update, container)
    if resumed:
        await respond(update, texts.alerts_resumed(), edit=False)
    text, keyboard = await views.start_view(container, actor)
    await respond(update, text, keyboard, edit=False)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/stop`` — deliver nothing to this chat until ``/start`` arrives.

    Available to everyone, administrators included, and never refused: being
    asked to stop messaging someone is not a privileged operation. The opt-out is
    stored in PostgreSQL, so it survives a redeploy.
    """
    container = get_container(context)
    await _set_alerts(update, container, False)
    await respond(update, texts.alerts_stopped(), edit=False)


async def _set_alerts(update: Update, container: AppContainer, value: bool) -> bool:
    """Persist the opt-in state. Returns True when it actually changed."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    try:
        was_subscribed = await container.admins.is_subscribed(user.id)
        await container.admins.set_subscription(user.id, chat.id, value)
    except Exception:
        log.exception("Could not change alert subscription", extra={"telegram_id": user.id})
        return False
    container.alerts.invalidate_recipients()
    return was_subscribed != value


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    container = get_container(context)
    actor = actor_of(update, container)
    if not actor.is_admin and not container.settings.config.public_mode:
        await respond(update, texts.PRIVATE_NOTICE, edit=False)
        return
    text, keyboard = await views.help_view(container, actor)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.VIEW_PUBLIC)
async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.about_view(container, actor)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.CHANGE_SETTINGS)
async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.panel_view(container, actor)
    await respond(update, text, keyboard, edit=False)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anything starting with ``/`` that no handler claimed."""
    container = get_container(context)
    actor = actor_of(update, container)
    if not actor.is_admin and not container.settings.config.public_mode:
        await respond(update, texts.PRIVATE_NOTICE, edit=False)
        return
    await respond(update, texts.unknown_command(), edit=False)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last line of defence: one bad update must never stop the bot (spec §26)."""
    log.exception("Unhandled error in a Telegram handler", exc_info=context.error)
    if not isinstance(update, Update):
        return
    try:
        chat = update.effective_chat
        query = update.callback_query
        if query is not None:
            await query.answer("Something went wrong. Please try again.", show_alert=True)
        elif chat is not None:
            await chat.send_message(texts.error_notice(), parse_mode="HTML")
    except Exception:
        # The failure was probably in sending; do not recurse.
        log.debug("Could not deliver the error notice")
