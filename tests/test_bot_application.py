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

from app.bot.application import build_application, post_init
from app.bot.handlers import COMMANDS, PUBLIC_COMMAND_MENU
from app.container import AppContainer


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
    """Enough of ``telegram.Bot`` for ``post_init``: identity + set_my_commands."""

    def __init__(self) -> None:
        self.id = 1234567890
        self.username = "stub_bot"
        self.published: list[Any] = []

    async def set_my_commands(self, commands: Any, **kwargs: Any) -> bool:
        self.published.append(commands)
        return True


def _stub_application(container: AppContainer) -> Any:
    return SimpleNamespace(bot=_StubBot(), bot_data={"app": container})


@pytest.mark.asyncio
async def test_post_init_publishes_the_command_menu(container: AppContainer):
    application = _stub_application(container)

    await post_init(application)

    assert application.bot.published == [PUBLIC_COMMAND_MENU]
    assert container.alerts.bot is application.bot


@pytest.mark.asyncio
async def test_post_init_is_idempotent(container: AppContainer):
    """``Runtime`` calls it explicitly; PTB would call it again under
    ``run_polling``. The menu must not be published twice."""
    application = _stub_application(container)

    await post_init(application)
    await post_init(application)

    assert len(application.bot.published) == 1


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
