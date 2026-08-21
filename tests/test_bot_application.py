"""Wiring of the real python-telegram-bot ``Application``.

This module exists because of a production defect. ``build_application`` was
covered by nothing: every other test drives handlers through the duck-typed
fakes in ``conftest``, so no test ever asked whether the assembled application
is actually connected to the alert service. It was not — ``attach_bot`` lived in
``post_init``, and PTB only calls ``post_init`` from ``run_polling`` /
``run_webhook``, while ``app.main.Runtime`` drives the lifecycle by hand. The
first live deploy logged "Alert dropped: Telegram bot not attached yet" for
every whale it found.

No network: ``ApplicationBuilder.build()`` performs no I/O, and nothing here
calls ``initialize()``. The token is the fictitious one from ``conftest``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from telegram import BotCommandScopeChat, BotCommandScopeDefault

from app.bot.application import build_application, post_init
from app.bot.commands import menu_for_role
from app.bot.handlers import (
    ADMIN_COMMAND_NAMES,
    COMMANDS,
    MAIN_ADMIN_COMMAND_NAMES,
    PUBLIC_COMMAND_MENU,
)
from app.container import AppContainer
from tests.conftest import MAIN_ADMIN_ID


# ── the regression ─────────────────────────────────────────────

def test_build_application_attaches_the_bot_to_the_alert_service(container: AppContainer):
    """The alert service must have a bot before any alert can be queued.

    ``AlertService._dispatch`` drops the alert outright when ``bot is None``, so
    this single assertion is the difference between a working bot and a silent
    one.
    """
    assert container.alerts.bot is None, "precondition: nothing attached yet"

    application = build_application(container)

    assert container.alerts.bot is application.bot


def test_the_container_is_published_for_handlers(container: AppContainer):
    application = build_application(container)
    assert application.bot_data["app"] is container


def test_every_command_is_registered(container: AppContainer):
    application = build_application(container)
    registered = {
        command
        for group in application.handlers.values()
        for handler in group
        for command in getattr(handler, "commands", ()) or ()
    }
    for name, _ in COMMANDS:
        assert name in registered, f"/{name} is in COMMANDS but not registered"


# ── post_init ──────────────────────────────────────────────────

class _StubBot:
    """Enough of ``telegram.Bot`` for ``post_init``: identity + set_my_commands.

    Records the scope as well as the menu, because the whole point of the scoped
    publish is *which* menu each audience gets (spec issue 4/14): asserting on
    the command lists alone would pass even if every audience got the admin menu.
    """

    def __init__(self) -> None:
        self.id = 1234567890
        self.username = "stub_bot"
        self.published: list[Any] = []
        self.scopes: list[Any] = []

    async def set_my_commands(self, commands: Any, **kwargs: Any) -> bool:
        self.published.append(commands)
        self.scopes.append(kwargs.get("scope"))
        return True


def _stub_application(container: AppContainer) -> Any:
    return SimpleNamespace(bot=_StubBot(), bot_data={"app": container})


@pytest.mark.asyncio
async def test_post_init_publishes_the_command_menu(container: AppContainer):
    application = _stub_application(container)

    await post_init(application)

    bot = application.bot
    # The default scope — everyone who has not been given a per-chat menu — sees
    # only the public commands.
    assert bot.published[0] == PUBLIC_COMMAND_MENU
    assert isinstance(bot.scopes[0], BotCommandScopeDefault)

    published = dict(zip(bot.scopes, bot.published))
    main = next(
        menu
        for scope, menu in published.items()
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == MAIN_ADMIN_ID
    )
    assert menu_for_role(main_admin=True, admin=True) == main
    assert container.alerts.bot is application.bot


@pytest.mark.asyncio
async def test_the_default_menu_leaks_no_admin_command(container: AppContainer):
    """Issue 4/5: a normal user's command list must not advertise admin controls."""
    application = _stub_application(container)

    await post_init(application)

    public = {command.command for command in application.bot.published[0]}
    assert not public & ADMIN_COMMAND_NAMES
    assert not public & MAIN_ADMIN_COMMAND_NAMES
    assert {"start", "help", "whales"} <= public


@pytest.mark.asyncio
async def test_post_init_is_idempotent(container: AppContainer):
    """``Runtime`` calls it explicitly; PTB would call it again under
    ``run_polling``. The menu must not be published twice."""
    application = _stub_application(container)

    await post_init(application)
    first = len(application.bot.published)
    await post_init(application)

    assert len(application.bot.published) == first


@pytest.mark.asyncio
async def test_post_init_survives_a_failed_menu_publish(container: AppContainer):
    """Publishing the menu is cosmetic and must never fail the deploy."""
    from telegram.error import TelegramError

    class Failing(_StubBot):
        async def set_my_commands(self, commands: Any, **kwargs: Any) -> bool:
            raise TelegramError("flood control")

    application = SimpleNamespace(bot=Failing(), bot_data={"app": container})

    await post_init(application)  # must not raise

    assert container.alerts.bot is application.bot
