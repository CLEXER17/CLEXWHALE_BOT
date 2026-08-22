"""Handler registration.

One place lists every command, so the command table, ``/help`` and the BotFather
command menu cannot drift apart.
"""

from __future__ import annotations

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot.commands import (
    ADMIN_COMMAND_NAMES,
    CO_ADMIN_COMMAND_MENU,
    MAIN_ADMIN_COMMAND_MENU,
    MAIN_ADMIN_COMMAND_NAMES,
    PUBLIC_COMMAND_MENU,
)
from app.bot.handlers import admin, callbacks, common, data, prompts

#: ``(command, handler)`` — the order only matters for readability.
COMMANDS = (
    ("start", common.cmd_start),
    ("stop", common.cmd_stop),
    ("help", common.cmd_help),
    ("about", common.cmd_about),
    ("panel", common.cmd_panel),
    # read-only
    ("status", data.cmd_status),
    ("whales", data.cmd_whales),
    ("recent", data.cmd_recent),
    ("orders", data.cmd_orders),
    ("positions", data.cmd_positions),
    ("coins", data.cmd_coins),
    ("stats", data.cmd_stats),
    ("wallets", data.cmd_wallets),
    # monitoring
    ("startmonitor", admin.cmd_startmonitor),
    ("stopmonitor", admin.cmd_stopmonitor),
    ("pause", admin.cmd_pause),
    ("go", admin.cmd_go),
    # filters
    ("threshold", admin.cmd_threshold),
    ("setthreshold", admin.cmd_setthreshold),
    ("margin", admin.cmd_margin),
    ("setmargin", admin.cmd_setmargin),
    ("cooldown", admin.cmd_cooldown),
    ("settings", admin.cmd_settings),
    ("config", admin.cmd_config),
    ("resetsettings", admin.cmd_resetsettings),
    ("setcoins", admin.cmd_setcoins),
    ("addcoin", admin.cmd_addcoin),
    ("removecoin", admin.cmd_removecoin),
    ("allcoins", admin.cmd_allcoins),
    # wallets
    ("watch", admin.cmd_watch),
    ("unwatch", admin.cmd_unwatch),
    # access
    ("public", admin.cmd_public),
    ("admins", admin.cmd_admins),
    ("addadmin", admin.cmd_addadmin),
    ("removeadmin", admin.cmd_removeadmin),
    ("audit", admin.cmd_audit),
)


def register_handlers(application: Application) -> None:
    for name, handler in COMMANDS:
        application.add_handler(CommandHandler(name, handler))

    application.add_handler(CallbackQueryHandler(callbacks.on_callback))

    # Answers to prompts, and a gentle nudge for anything else typed in DM.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, prompts.on_text)
    )

    # Anything else beginning with "/" that no command claimed.
    application.add_handler(MessageHandler(filters.COMMAND, common.cmd_unknown))

    application.add_error_handler(common.on_error)


__all__ = [
    "ADMIN_COMMAND_NAMES",
    "CO_ADMIN_COMMAND_MENU",
    "COMMANDS",
    "MAIN_ADMIN_COMMAND_MENU",
    "MAIN_ADMIN_COMMAND_NAMES",
    "PUBLIC_COMMAND_MENU",
    "register_handlers",
]
