"""Administrative commands.

Every handler here is guarded by a capability, and the capability is checked
against ``update.effective_user.id`` — not against anything the caller sent. The
main-admin-only actions (:data:`Capability.MANAGE_ADMINS`,
:data:`Capability.VIEW_AUDIT`) are refused for co-admins by the same code path
that refuses ordinary users, so there is no weaker route to them.
"""

from __future__ import annotations

import re

from telegram import Update
from telegram.ext import ContextTypes

from app.bot import views
from app.bot.commands import sync_command_menus
from app.bot.keyboards import inline
from app.bot.messages import texts
from app.bot.middleware.permissions import get_container, requires, respond
from app.services.admin_service import Actor, AdminError, Capability
from app.services.settings_service import MAX_THRESHOLD, MIN_THRESHOLD
from app.utils.formatting import is_hex_address
from app.utils.logging import get_logger

log = get_logger(__name__)

_SUFFIXES = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}
_NUMBER_RE = re.compile(r"^\$?([0-9]+(?:[.,][0-9]+)*)\s*([KMB])?$", re.IGNORECASE)
_COIN_RE = re.compile(r"^[A-Z0-9]{1,12}$")


# ── parsing ────────────────────────────────────────────────────
def parse_usd(raw: str) -> float | None:
    """``2000000``, ``2,000,000``, ``$2m`` and ``2M`` all mean the same thing."""
    text = raw.strip().replace("_", "")
    match = _NUMBER_RE.match(text)
    if not match:
        return None
    digits, suffix = match.group(1), (match.group(2) or "").upper()
    # A comma is a thousands separator; a dot is a decimal point.
    digits = digits.replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    if suffix:
        value *= _SUFFIXES[suffix]
    return value if value > 0 else None


#: Words that turn a gate off. ``parse_usd`` rejects zero on purpose — a $0
#: whale threshold is a mistake — but a $0 *margin* gate is how you disable it.
_OFF_WORDS = frozenset({"0", "off", "none", "no", "disable", "disabled"})


def parse_margin(raw: str) -> float | None:
    """Like :func:`parse_usd`, except ``0`` / ``off`` is valid and means "no gate"."""
    text = raw.strip().lower().lstrip("$").replace(",", "").replace("_", "")
    if text in _OFF_WORDS:
        return 0.0
    try:
        if float(text) == 0:
            return 0.0
    except ValueError:
        pass
    return parse_usd(raw)


def parse_coins(args: list[str]) -> tuple[list[str], list[str]]:
    """Split ``BTC eth, sol`` into (valid, invalid)."""
    valid: list[str] = []
    invalid: list[str] = []
    for token in " ".join(args).replace(",", " ").split():
        symbol = token.strip().upper()
        if not symbol:
            continue
        if _COIN_RE.match(symbol):
            if symbol not in valid:
                valid.append(symbol)
        else:
            invalid.append(token)
    return valid, invalid


def parse_on_off(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in {"on", "true", "1", "yes", "enable", "enabled"}:
        return True
    if value in {"off", "false", "0", "no", "disable", "disabled"}:
        return False
    return None


def parse_user_id(raw: str) -> int | None:
    text = raw.strip()
    if text.startswith("@"):
        return None  # A username is not a stable identifier; we store the ID.
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


# ── monitoring (spec §10) ──────────────────────────────────────
@requires(Capability.CONTROL_MONITORING)
async def cmd_startmonitor(
    update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor
) -> None:
    container = get_container(context)
    await container.settings.set_monitoring(True, actor.telegram_id)
    await respond(update, texts.monitoring_started(), edit=False)
    text, keyboard = await views.status_view(container)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.CONTROL_MONITORING)
async def cmd_stopmonitor(
    update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor
) -> None:
    container = get_container(context)
    await container.settings.set_monitoring(False, actor.telegram_id)
    await respond(update, texts.monitoring_stopped(), edit=False)
    text, keyboard = await views.status_view(container)
    await respond(update, text, keyboard, edit=False)


# ── global pause / resume ──────────────────────────────────────
@requires(Capability.CONTROL_MONITORING, allow_when_paused=True)
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    """Stop the bot doing anything at all until /go.

    Wider than /stopmonitor: that only turns the detectors off, this also
    withholds every alert and refuses every other command. The
    ``monitoring_enabled`` setting is left untouched, so /go restores whatever
    was configured before the pause.
    """
    container = get_container(context)
    await container.settings.set_paused(True, actor.telegram_id)
    await respond(update, texts.paused_confirmation(), edit=False)


@requires(Capability.CONTROL_MONITORING, allow_when_paused=True)
async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    """Lift the global pause."""
    container = get_container(context)
    was_paused = container.settings.config.paused
    await container.settings.set_paused(False, actor.telegram_id)
    await respond(update, texts.resumed_confirmation(was_paused), edit=False)
    text, keyboard = await views.status_view(container)
    await respond(update, text, keyboard, edit=False)


# ── threshold (spec §8) ────────────────────────────────────────
@requires(Capability.CHANGE_THRESHOLD)
async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.threshold_view(container)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.CHANGE_THRESHOLD)
async def cmd_setthreshold(
    update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor
) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        text, keyboard = await views.threshold_view(container)
        await respond(update, text, keyboard, edit=False)
        return

    requested = parse_usd(" ".join(args))
    if requested is None:
        await respond(update, texts.invalid_number(" ".join(args), "2000000"), edit=False)
        return

    applied = await container.settings.set_threshold(requested, actor.telegram_id)
    clamped = abs(applied - requested) > 0.5
    await respond(update, texts.threshold_updated(applied, clamped), edit=False)


# ── margin gate ────────────────────────────────────────────────
@requires(Capability.CHANGE_THRESHOLD)
async def cmd_margin(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.margin_view(container)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.CHANGE_THRESHOLD)
async def cmd_setmargin(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        text, keyboard = await views.margin_view(container)
        await respond(update, text, keyboard, edit=False)
        return

    requested = parse_margin(" ".join(args))
    if requested is None:
        await respond(update, texts.invalid_number(" ".join(args), "2000000"), edit=False)
        return

    applied = await container.settings.set_margin(requested, actor.telegram_id)
    clamped = requested > 0 and abs(applied - requested) > 0.5
    await respond(update, texts.margin_updated(applied, clamped), edit=False)


@requires(Capability.CHANGE_SETTINGS)
async def cmd_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        text, keyboard = await views.cooldown_view(container)
        await respond(update, text, keyboard, edit=False)
        return
    try:
        seconds = int(args[0])
    except ValueError:
        await respond(update, texts.invalid_number(args[0], "30"), edit=False)
        return
    applied = await container.settings.set_cooldown(seconds, actor.telegram_id)
    await respond(update, texts.cooldown_updated(applied), edit=False)


@requires(Capability.CHANGE_SETTINGS)
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.settings_view(container)
    await respond(update, text, keyboard, edit=False)


# ── coins (spec §7) ────────────────────────────────────────────
def unknown_coins(container, coins: list[str]) -> list[str]:
    """Coins Hyperliquid does not list as perpetuals (empty universe → trust the user)."""
    universe = set(container.engine.known_coins)
    if not universe:
        return []
    return [coin for coin in coins if coin not in universe]


@requires(Capability.CHANGE_COINS)
async def cmd_setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        text, keyboard = await views.coins_view(container, actor)
        await respond(update, text, keyboard, edit=False)
        return

    coins, invalid = parse_coins(args)
    if invalid:
        await respond(update, texts.invalid_coin(invalid[0]), edit=False)
        return
    if not coins:
        await respond(update, texts.invalid_coin(" ".join(args)), edit=False)
        return

    unknown = unknown_coins(container, coins)
    # The one coin command that legitimately replaces the list, because the verb
    # is "set". Every other path adds or removes a single coin (spec §3/§34).
    await container.settings.set_coins(coins, actor.telegram_id)
    added, removed = container.settings.last_coin_diff
    # Leaving ALL COINS on would make an explicit selection meaningless.
    if container.settings.config.all_coins:
        await container.settings.set_value("all_coins", False, actor.telegram_id)

    await respond(
        update,
        texts.coins_replaced(container.settings.config, added, removed),
        edit=False,
    )
    for coin in unknown:
        await respond(update, texts.unknown_coin(coin), edit=False)


@requires(Capability.CHANGE_COINS)
async def cmd_addcoin(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        await respond(update, "Usage: <code>/addcoin XRP</code>", edit=False)
        return
    coins, invalid = parse_coins(args)
    if invalid or not coins:
        await respond(update, texts.invalid_coin(args[0]), edit=False)
        return

    # A pure PATCH: each coin is inserted on its own row, so BTC/ETH/SOL are
    # untouched by /addcoin HYPE and a repeated /addcoin HYPE is a no-op rather
    # than a duplicate (spec §3).
    added, present = await container.settings.add_coins(coins, actor.telegram_id)
    unknown = unknown_coins(container, coins)

    await respond(
        update,
        texts.coins_added(container.settings.config, added, present),
        edit=False,
    )
    for coin in unknown:
        await respond(update, texts.unknown_coin(coin), edit=False)


@requires(Capability.CHANGE_COINS)
async def cmd_removecoin(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        await respond(update, "Usage: <code>/removecoin SOL</code>", edit=False)
        return
    coins, invalid = parse_coins(args)
    if invalid or not coins:
        await respond(update, texts.invalid_coin(args[0]), edit=False)
        return

    removed = [coin for coin in coins if await container.settings.remove_coin(coin, actor.telegram_id)]
    if removed:
        await respond(update, texts.coins_updated(container.settings.config), edit=False)
    else:
        await respond(update, "ℹ️ That coin was not in the list.", edit=False)


@requires(Capability.CHANGE_COINS)
async def cmd_allcoins(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        text, keyboard = await views.coins_view(container, actor)
        await respond(update, text, keyboard, edit=False)
        return
    value = parse_on_off(args[0])
    if value is None:
        await respond(update, "Usage: <code>/allcoins on</code> or <code>/allcoins off</code>", edit=False)
        return
    await container.settings.set_value("all_coins", value, actor.telegram_id)
    text, keyboard = await views.coins_view(container, actor)
    await respond(update, text, keyboard, edit=False)


# ── public mode (spec §11) ─────────────────────────────────────
@requires(Capability.CHANGE_PUBLIC_MODE)
async def cmd_public(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        text, keyboard = await views.public_mode_view(container)
        await respond(update, text, keyboard, edit=False)
        return
    value = parse_on_off(args[0])
    if value is None:
        await respond(update, "Usage: <code>/public on</code> or <code>/public off</code>", edit=False)
        return
    await container.settings.set_public_mode(value, actor.telegram_id)
    container.alerts.invalidate_recipients()
    text, keyboard = await views.public_mode_view(container)
    await respond(update, text, keyboard, edit=False)


# ── tracked wallets ────────────────────────────────────────────
@requires(Capability.MANAGE_WALLETS)
async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        await respond(update, texts.PROMPT_WALLET, inline.cancel_prompt(), edit=False)
        context.user_data["pending"] = {"kind": "wallet"}
        return
    address = args[0].strip()
    if not is_hex_address(address):
        await respond(update, texts.invalid_wallet(address), edit=False)
        return
    added = await container.settings.add_tracked_wallet(address, actor.telegram_id)
    message = (
        f"✅ Watching <code>{address.lower()}</code>. Its positions and resting "
        "orders will be enriched continuously."
        if added
        else "ℹ️ That wallet is already being watched."
    )
    await respond(update, message, edit=False)


@requires(Capability.MANAGE_WALLETS)
async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        await respond(update, "Usage: <code>/unwatch 0x…</code>", edit=False)
        return
    address = args[0].strip()
    if not is_hex_address(address):
        await respond(update, texts.invalid_wallet(address), edit=False)
        return
    removed = await container.settings.remove_tracked_wallet(address, actor.telegram_id)
    await respond(
        update,
        "✅ Stopped watching that wallet." if removed else "ℹ️ That wallet was not watched.",
        edit=False,
    )


# ── admin management (spec §13–§15) ────────────────────────────
@requires(Capability.VIEW_ADMINS)
async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.admin_home_view(container, actor)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.MANAGE_ADMINS)
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        context.user_data["pending"] = {"kind": "admin_add"}
        await respond(update, texts.PROMPT_ADD_ADMIN, inline.cancel_prompt(), edit=False)
        return
    await apply_add_admin(update, context, actor, args[0])


async def apply_add_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor, raw: str
) -> None:
    container = get_container(context)
    target = parse_user_id(raw)
    if target is None:
        await respond(update, texts.invalid_user_id(raw), edit=False)
        return
    try:
        await container.admins.add_co_admin(actor.telegram_id, target)
    except AdminError as exc:
        await respond(update, str(exc), edit=False)
        return
    container.alerts.invalidate_recipients()
    await respond(update, texts.co_admin_added(target), edit=False)
    await sync_command_menus(context.bot, container)
    await _notify_role_change(context, target, texts.promoted_notice())


@requires(Capability.MANAGE_ADMINS)
async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    args = context.args or []
    if not args:
        context.user_data["pending"] = {"kind": "admin_remove"}
        await respond(update, texts.PROMPT_REMOVE_ADMIN, inline.cancel_prompt(), edit=False)
        return
    await apply_remove_admin(update, context, actor, args[0])


async def apply_remove_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor, raw: str
) -> None:
    container = get_container(context)
    target = parse_user_id(raw)
    if target is None:
        await respond(update, texts.invalid_user_id(raw), edit=False)
        return
    try:
        await container.admins.remove_co_admin(actor.telegram_id, target)
    except AdminError as exc:
        await respond(update, str(exc), edit=False)
        return
    container.alerts.invalidate_recipients()
    await respond(update, texts.co_admin_removed(target), edit=False)
    # Delete their chat scope: Telegram would otherwise keep showing them the
    # admin commands they can no longer use.
    await sync_command_menus(context.bot, container, demoted=target)
    await _notify_role_change(context, target, texts.demoted_notice())


async def _notify_role_change(
    context: ContextTypes.DEFAULT_TYPE, telegram_id: int, message: str
) -> None:
    """Best effort — the target may never have started the bot."""
    try:
        await context.bot.send_message(telegram_id, message, parse_mode="HTML")
    except Exception as exc:
        log.info(
            "Could not notify the affected administrator",
            extra={"telegram_id": telegram_id, "reason": type(exc).__name__},
        )


@requires(Capability.VIEW_AUDIT)
async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    container = get_container(context)
    text, keyboard = await views.audit_view(container)
    await respond(update, text, keyboard, edit=False)


# ── persistence (spec §27, §23) ────────────────────────────────
@requires(Capability.CHANGE_SETTINGS)
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor) -> None:
    """Show the stored configuration, read from the database rather than the cache."""
    container = get_container(context)
    text, keyboard = await views.config_view(container)
    await respond(update, text, keyboard, edit=False)


@requires(Capability.RESET_SETTINGS)
async def cmd_resetsettings(
    update: Update, context: ContextTypes.DEFAULT_TYPE, actor: Actor
) -> None:
    """Two-step reset. Step one only *describes* what would be lost (spec §23).

    ``/resetsettings`` on its own never changes anything; the confirmation button
    carries the actual reset. No ordinary settings command may reach this path.
    """
    container = get_container(context)
    await respond(
        update,
        texts.confirm_reset_settings(container.settings.config),
        inline.confirm_reset_settings(),
        edit=False,
    )


__all__ = [
    "MAX_THRESHOLD",
    "MIN_THRESHOLD",
    "apply_add_admin",
    "apply_remove_admin",
    "cmd_addadmin",
    "cmd_addcoin",
    "cmd_admins",
    "cmd_allcoins",
    "cmd_audit",
    "cmd_config",
    "cmd_cooldown",
    "cmd_margin",
    "cmd_public",
    "cmd_removeadmin",
    "cmd_removecoin",
    "cmd_resetsettings",
    "cmd_setcoins",
    "cmd_setmargin",
    "cmd_settings",
    "cmd_setthreshold",
    "cmd_startmonitor",
    "cmd_stopmonitor",
    "cmd_threshold",
    "cmd_unwatch",
    "cmd_watch",
    "parse_coins",
    "parse_margin",
    "parse_on_off",
    "parse_usd",
    "parse_user_id",
]
