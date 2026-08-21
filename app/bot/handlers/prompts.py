"""The prompt flow.

Some panels ask for a value ("Send the Telegram User ID to add as Co-Admin.").
The pending request is stored per user in ``context.user_data`` and consumed by
the next plain text message from that same user.

The capability is re-checked when the answer arrives, not only when the prompt
was issued: an administrator can be demoted between the two, and the prompt must
not become a stale grant of authority.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.bot import views
from app.bot.handlers import admin as admin_cmds
from app.bot.messages import texts
from app.bot.middleware.permissions import (
    actor_of,
    get_container,
    refuse,
    respond,
    throttle,
)
from app.services.admin_service import AdminError, Capability
from app.utils.formatting import is_hex_address
from app.utils.logging import get_logger

log = get_logger(__name__)

#: What each pending kind is allowed to change.
_REQUIRED = {
    "threshold": Capability.CHANGE_THRESHOLD,
    "margin": Capability.CHANGE_THRESHOLD,
    "cooldown": Capability.CHANGE_SETTINGS,
    "coins": Capability.CHANGE_COINS,
    "admin_add": Capability.MANAGE_ADMINS,
    "admin_remove": Capability.MANAGE_ADMINS,
    "wallet": Capability.MANAGE_WALLETS,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a plain text message: either an answer to a prompt, or a nudge."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return

    container = get_container(context)
    pending = (context.user_data or {}).get("pending")

    if not pending:
        actor = actor_of(update, container)
        if not actor.is_admin and not container.settings.config.public_mode:
            await respond(update, texts.PRIVATE_NOTICE, edit=False)
            return
        text, keyboard = await views.start_view(container, actor)
        await respond(update, text, keyboard, edit=False)
        return

    if not throttle(user.id):
        await respond(update, texts.rate_limited(), edit=False)
        return

    kind = str(pending.get("kind") or "")
    capability = _REQUIRED.get(kind)
    if capability is None:
        context.user_data.pop("pending", None)
        return

    try:
        actor = container.admins.require(
            user.id, capability, public_mode=container.settings.config.public_mode
        )
    except AdminError as exc:
        context.user_data.pop("pending", None)
        await refuse(update, str(exc))
        return

    if container.settings.config.paused:
        # A prompt outlives the command that opened it, so a pause can land
        # between the question and the answer. Drop the prompt rather than apply
        # a setting change while everything else is frozen.
        context.user_data.pop("pending", None)
        await refuse(update, texts.paused_notice(admin=actor.is_admin))
        return

    raw = message.text.strip()
    if raw.lower() in {"cancel", "/cancel"}:
        context.user_data.pop("pending", None)
        await respond(update, texts.CANCELLED, edit=False)
        return

    # Consume the prompt before acting, so a failure does not leave the user
    # stuck in a state where their next message is reinterpreted.
    context.user_data.pop("pending", None)

    if kind == "threshold":
        requested = admin_cmds.parse_usd(raw)
        if requested is None:
            await respond(update, texts.invalid_number(raw, "2000000"), edit=False)
            return
        applied = await container.settings.set_threshold(requested, actor.telegram_id)
        await respond(
            update,
            texts.threshold_updated(applied, abs(applied - requested) > 0.5),
            edit=False,
        )
        text, keyboard = await views.threshold_view(container)
        await respond(update, text, keyboard, edit=False)
        return

    if kind == "margin":
        requested = admin_cmds.parse_margin(raw)
        if requested is None:
            await respond(update, texts.invalid_number(raw, "2000000"), edit=False)
            return
        applied = await container.settings.set_margin(requested, actor.telegram_id)
        clamped = requested > 0 and abs(applied - requested) > 0.5
        await respond(update, texts.margin_updated(applied, clamped), edit=False)
        text, keyboard = await views.margin_view(container)
        await respond(update, text, keyboard, edit=False)
        return

    if kind == "cooldown":
        try:
            seconds = int(raw)
        except ValueError:
            await respond(update, texts.invalid_number(raw, "30"), edit=False)
            return
        applied = await container.settings.set_cooldown(seconds, actor.telegram_id)
        await respond(update, texts.cooldown_updated(applied), edit=False)
        return

    if kind == "coins":
        coins, invalid = admin_cmds.parse_coins([raw])
        if invalid or not coins:
            await respond(update, texts.invalid_coin(raw), edit=False)
            return
        unknown = admin_cmds.unknown_coins(container, coins)
        await container.settings.set_coins(coins, actor.telegram_id)
        if container.settings.config.all_coins:
            await container.settings.set_value("all_coins", False, actor.telegram_id)
        await respond(update, texts.coins_updated(container.settings.config), edit=False)
        for coin in unknown:
            await respond(update, texts.unknown_coin(coin), edit=False)
        return

    if kind == "admin_add":
        await admin_cmds.apply_add_admin(update, context, actor, raw)
        return

    if kind == "admin_remove":
        await admin_cmds.apply_remove_admin(update, context, actor, raw)
        return

    if kind == "wallet":
        if not is_hex_address(raw):
            await respond(update, texts.invalid_wallet(raw), edit=False)
            return
        added = await container.settings.add_tracked_wallet(raw, actor.telegram_id)
        await respond(
            update,
            f"✅ Watching <code>{raw.lower()}</code>."
            if added
            else "ℹ️ That wallet is already being watched.",
            edit=False,
        )
