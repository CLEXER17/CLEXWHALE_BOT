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
from telegram.ext import AIORateLimiter, Application, ApplicationBuilder, Defaults

from app.bot.commands import publish_command_menus
from app.bot.handlers import register_handlers
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
        .post_init(post_init)
        .build()
    )

    application.bot_data["app"] = container
    # Attach the bot here, not in ``post_init``. ``application.bot`` exists the
    # moment the builder returns, and deferring it was a production bug: PTB
    # calls ``post_init`` only from ``run_polling``/``run_webhook``, while this
    # process drives the lifecycle step by step (``app.main.Runtime.start``). The
    # alert service was therefore left with ``bot is None`` and every whale alert
    # was discarded with "Telegram bot not attached yet".
    container.alerts.attach_bot(application.bot)
    register_handlers(application)
    return application


async def post_init(application: Application) -> None:
    """Work that needs the bot's own identity — run after ``initialize()``.

    PTB invokes this itself only from ``run_polling``/``run_webhook``, so
    :class:`app.main.Runtime` calls it explicitly. The builder registration is
    kept so the function still runs if anyone switches to ``run_polling``, and
    the guard below makes the resulting double call harmless.

    Publishing the command menu is cosmetic: a failure is logged, never fatal.
    """
    if application.bot_data.get("_post_init_done"):
        return
    application.bot_data["_post_init_done"] = True

    container: AppContainer = application.bot_data["app"]
    # Idempotent, and already done in ``build_application``; repeated so this
    # function remains correct on its own if it is ever the only caller.
    container.alerts.attach_bot(application.bot)

    me = application.bot
    log.info(
        "Telegram connected",
        extra={"bot_username": me.username, "bot_id": me.id},
    )

    await publish_command_menus(application.bot, container)


__all__ = ["build_application", "post_init"]
