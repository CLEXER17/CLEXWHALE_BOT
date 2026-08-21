"""Command menus and the Telegram *scopes* they are published under.

Task "ADMIN UI + WALLET DISPLAY + DATA INTEGRITY" issues 4, 5 and 14: an
ordinary user must not even see the administrative commands. Hiding them from
``/help`` is not enough — Telegram's ✚ menu is populated by ``setMyCommands``,
so the menu itself has to be scoped:

* :class:`~telegram.BotCommandScopeDefault` gets :data:`PUBLIC_COMMAND_MENU`.
* each co-admin's private chat gets :data:`CO_ADMIN_COMMAND_MENU`.
* the main admin's private chat gets :data:`MAIN_ADMIN_COMMAND_MENU`.

**Visibility is not authority.** Every handler re-derives the caller's role from
``update.effective_user.id`` and refuses anything that role does not carry, so a
user who types a hidden command, or forges its ``callback_data``, is still
refused server-side. This module only decides what is advertised.

Kept out of :mod:`app.bot.handlers` and :mod:`app.bot.application` so both — and
the admin handlers that re-publish after a role change — can import it without a
cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Bot, BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import TelegramError

from app.utils.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.container import AppContainer

log = get_logger(__name__)

#: What every user sees. No administrative command appears here: advertising a
#: control an ordinary user cannot use is noise *and* discloses the privileged
#: surface (and, through it, that there is an admin to social-engineer).
#:
#: The membership test is the handler's capability, not intuition: everything
#: here is gated on :data:`PUBLIC_CAPABILITIES`. ``/recent`` sat in the co-admin
#: block while its handler required only ``VIEW_WHALES`` and ``help_text``
#: advertised it under "Whale data" — so an ordinary user was told about a
#: command missing from their ✚ menu, and the three sources of truth disagreed.
PUBLIC_COMMAND_MENU: tuple[BotCommand, ...] = (
    BotCommand("start", "Open the whale monitor"),
    BotCommand("stop", "Stop receiving alerts"),
    BotCommand("help", "List the available commands"),
    BotCommand("status", "Monitoring status"),
    BotCommand("whales", "Recent whale events"),
    BotCommand("recent", "Latest signals"),
    BotCommand("orders", "Large resting orders"),
    BotCommand("positions", "Tracked open positions"),
    BotCommand("coins", "Monitored coins"),
)

#: Everything a co-admin may actually invoke (``CO_ADMIN_CAPABILITIES``).
CO_ADMIN_EXTRA: tuple[BotCommand, ...] = (
    BotCommand("panel", "Open the control panel"),
    BotCommand("pause", "Pause the bot entirely"),
    BotCommand("go", "Resume the bot"),
    BotCommand("startmonitor", "Start whale monitoring"),
    BotCommand("stopmonitor", "Stop whale monitoring"),
    BotCommand("settings", "Show every setting"),
    BotCommand("threshold", "Show the alert thresholds"),
    BotCommand("setthreshold", "Set the minimum whale value"),
    BotCommand("margin", "Show the margin gate"),
    BotCommand("setmargin", "Set the minimum margin at risk"),
    BotCommand("cooldown", "Set the per-wallet alert cooldown"),
    BotCommand("setcoins", "Replace the monitored coin list"),
    BotCommand("addcoin", "Add a coin to the list"),
    BotCommand("removecoin", "Remove a coin from the list"),
    BotCommand("allcoins", "Monitor every coin"),
    BotCommand("watch", "Watch a wallet address"),
    BotCommand("unwatch", "Stop watching a wallet"),
    BotCommand("wallets", "Watched and most active wallets"),
    BotCommand("stats", "Statistics and diagnostics"),
    BotCommand("public", "Turn public mode on or off"),
    BotCommand("admins", "Show the admin roster"),
)

#: Main-admin only (``MAIN_ONLY_CAPABILITIES``): a co-admin never sees these.
MAIN_ADMIN_EXTRA: tuple[BotCommand, ...] = (
    BotCommand("addadmin", "Add a co-admin"),
    BotCommand("removeadmin", "Remove a co-admin"),
    BotCommand("audit", "Recent configuration changes"),
)

CO_ADMIN_COMMAND_MENU: tuple[BotCommand, ...] = PUBLIC_COMMAND_MENU + CO_ADMIN_EXTRA
MAIN_ADMIN_COMMAND_MENU: tuple[BotCommand, ...] = CO_ADMIN_COMMAND_MENU + MAIN_ADMIN_EXTRA

#: Names that must never appear in the default (everyone) scope.
ADMIN_COMMAND_NAMES = frozenset(c.command for c in CO_ADMIN_EXTRA + MAIN_ADMIN_EXTRA)
MAIN_ADMIN_COMMAND_NAMES = frozenset(c.command for c in MAIN_ADMIN_EXTRA)


def menu_for_role(*, main_admin: bool, admin: bool) -> tuple[BotCommand, ...]:
    if main_admin:
        return MAIN_ADMIN_COMMAND_MENU
    if admin:
        return CO_ADMIN_COMMAND_MENU
    return PUBLIC_COMMAND_MENU


async def publish_command_menus(bot: Bot, container: AppContainer) -> dict[str, int]:
    """Write the default scope and one chat scope per administrator.

    Returns a per-scope count. A failure is logged and never fatal: the bot runs
    with a stale menu, and the next publish (startup, or the next role change)
    corrects it. A chat scope is rejected by Telegram until that user has opened
    a private chat with the bot, which is normal for a freshly added co-admin.
    """
    published = {"default": 0, "admin": 0, "failed": 0}
    try:
        await bot.set_my_commands(PUBLIC_COMMAND_MENU, scope=BotCommandScopeDefault())
        published["default"] += 1
    except TelegramError as exc:
        published["failed"] += 1
        log.warning("Could not publish the default command menu", extra={"error": str(exc)})

    for telegram_id in container.admins.admin_ids:
        menu = menu_for_role(
            main_admin=container.admins.is_main_admin(telegram_id), admin=True
        )
        try:
            await bot.set_my_commands(menu, scope=BotCommandScopeChat(chat_id=telegram_id))
            published["admin"] += 1
        except TelegramError as exc:
            published["failed"] += 1
            log.debug(
                "Could not publish an admin command menu",
                extra={"telegram_id": telegram_id, "error": str(exc)},
            )
    log.info("Command menus published", extra=published)
    return published


async def revoke_command_menu(bot: Bot, telegram_id: int) -> None:
    """Send a demoted user back to the default menu.

    Telegram keeps a chat scope until it is deleted, so a removed co-admin would
    keep seeing the admin commands in their ✚ menu — refused if typed, but still
    disclosure. Deleting the chat scope makes Telegram fall back to the default.
    """
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=telegram_id))
    except TelegramError as exc:
        log.debug(
            "Could not reset a command menu",
            extra={"telegram_id": telegram_id, "error": str(exc)},
        )


async def sync_command_menus(
    bot: Bot | None, container: AppContainer, *, demoted: int | None = None
) -> None:
    """Re-publish after a role change. Safe to call with no bot attached."""
    if bot is None:  # pragma: no cover - only before Telegram is up
        return
    if demoted is not None:
        await revoke_command_menu(bot, demoted)
    await publish_command_menus(bot, container)


__all__ = [
    "ADMIN_COMMAND_NAMES",
    "CO_ADMIN_COMMAND_MENU",
    "CO_ADMIN_EXTRA",
    "MAIN_ADMIN_COMMAND_MENU",
    "MAIN_ADMIN_COMMAND_NAMES",
    "MAIN_ADMIN_EXTRA",
    "PUBLIC_COMMAND_MENU",
    "menu_for_role",
    "publish_command_menus",
    "revoke_command_menu",
    "sync_command_menus",
]
