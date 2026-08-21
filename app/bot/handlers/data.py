"""Read-only data commands.

Everything here is available to an ordinary user when public mode is on and to
administrators always — the ``VIEW_WHALES`` capability encodes exactly that.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from app.bot import views
from app.bot.middleware.permissions import get_container, requires, respond
from app.services.admin_service import Actor, Capability


def _coin_arg(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """First argument as a coin symbol, if it looks like one."""
    args = context.args or []
    if not args:
        return None
    candidate = args[0].strip().upper()
    if candidate.isalnum() and len(candidate) <= 12:
        return candidate
    return None


@requires(Capability.VIEW_WHALES, allow_when_paused=True)
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    """Admins get the operational panel; users get the signal feed summary.

    Exempt from the global pause: this is the one view that explains *why*
    everything else is refused, and it changes nothing.
    """
    container = get_container(context)
    if actor.is_admin:
        text, keyboard = await views.status_view(container)
    else:
        text, keyboard = await views.live_view(container, actor)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.VIEW_WHALES)
async def cmd_whales(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.whales_view(container, actor, _coin_arg(context))
    await respond(update, text, keyboard, edit=False)


@requires(Capability.VIEW_WHALES)
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.live_view(container, actor)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.VIEW_WHALES)
async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.orders_view(container, actor, _coin_arg(context))
    await respond(update, text, keyboard, edit=False)


@requires(Capability.VIEW_WHALES)
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.positions_view(container, actor, _coin_arg(context))
    await respond(update, text, keyboard, edit=False)


@requires(Capability.VIEW_WHALES)
async def cmd_coins(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.coins_view(container, actor)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.VIEW_STATS)
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.stats_view(container, actor)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.MANAGE_WALLETS)
async def cmd_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.wallets_view(container, actor)
    await respond(update, text, keyboard, edit=False)
