"""Construction of the python-telegram-bot :class:`Application`.

Kept separate from :mod:`app.main` so the wiring can be exercised without
starting a process: build the application, assert the handlers are registered,
throw it away.

The container is published on ``bot_data["app"]`` — that is how every handler
reaches the database, the settings service and the whale engine (see
:func:`app.bot.middleware.permissions.get_container`). Nothing imports a global.
"""

from __future__ import annotations

from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import AIORateLimiter, Application, ApplicationBuilder, Defaults

from app.bot.handlers import PUBLIC_COMMAND_MENU, register_handlers
from app.container import AppContainer
from app.utils.logging import get_logger

log = get_logger(__name__)

#: Telegram HTTP timeouts (seconds). Generous enough for a slow network, short
#: enough that a stuck request cannot wedge the alert sender for minutes.
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 20.0
_WRITE_TIMEOUT = 20.0
_POOL_TIMEOUT = 10.0


def build_application(container: AppContainer) -> Application:
    """Create the Telegram application for ``container``.

    The token comes from the validated environment only; it is never written to
    a log line, a message or a database row.
    """
    # Handlers stay blocking (PTB's default): updates are processed one at a
    # time, so the pending-prompt state in ``user_data`` cannot be read and
    # written by two overlapping tasks. Alerts are sent from the alert
    # service's own task, so a slow handler never delays them.
    defaults = Defaults(
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )

    application = (
        ApplicationBuilder()
        .token(container.env.bot_token)
        .defaults(defaults)
        # Serialises outgoing calls against Telegram's documented limits so a
        # burst of whale alerts is queued rather than rejected with a 429.
        .rate_limiter(AIORateLimiter())
        .connect_timeout(_CONNECT_TIMEOUT)
        .read_timeout(_READ_TIMEOUT)
        .write_timeout(_WRITE_TIMEOUT)
        .pool_timeout(_POOL_TIMEOUT)
        .get_updates_read_timeout(_READ_TIMEOUT)
        .post_init(_post_init)
        .build()
    )

    application.bot_data["app"] = container
    register_handlers(application)
    return application


async def _post_init(application: Application) -> None:
    """Runs after the bot's own identity has been fetched.

    Two jobs: give the alert service a bot to send with, and publish the command
    menu. Both are safe to retry, and a failure to publish the menu is cosmetic —
    it must not stop the deploy.
    """
    container: AppContainer = application.bot_data["app"]
    container.alerts.attach_bot(application.bot)

    me = application.bot
    log.info(
        "Telegram connected",
        extra={"bot_username": me.username, "bot_id": me.id},
    )

    try:
        await application.bot.set_my_commands(PUBLIC_COMMAND_MENU)
    except TelegramError as exc:
        log.warning("Could not publish the command menu", extra={"error": str(exc)})


__all__ = ["build_application"]
