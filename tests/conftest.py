"""Shared fixtures.

Three rules shape everything here:

1. **No network.** Not one test touches Hyperliquid or Telegram. The two seams
   that make that possible are ``HyperliquidREST._client`` (only assigned in
   ``start()`` when it is ``None``) and the module-level
   ``app.hyperliquid.websocket.ws_connect`` symbol.
2. **No developer environment leaks in.** ``Settings`` reads ``.env`` and the OS
   environment by default, so a real ``BOT_TOKEN`` on the machine would change
   test outcomes. An autouse fixture scrubs every variable named after a
   ``Settings`` field and passes ``_env_file=None``.
3. **No shared process state between tests.** ``permissions`` keeps a
   module-level token bucket and a registration cache; both are cleared.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from app.bot.middleware import permissions
from app.config import Settings
from app.container import AppContainer
from app.database.base import Database, set_database

MAIN_ADMIN_ID = 700_000_001
CO_ADMIN_ID = 700_000_002
STRANGER_ID = 700_000_003

#: Shape-valid but entirely fictitious — matches ``_BOT_TOKEN_RE`` and belongs to
#: no real bot. Never a real credential, in tests or anywhere else.
FAKE_BOT_TOKEN = "1234567890:TESTTESTTESTTESTTESTTESTTESTTESTTES"

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


# ── environment isolation ──────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's real environment from every test."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture(autouse=True)
def _reset_permission_state() -> Any:
    """Command throttling and the user-registration cache are process-level."""
    permissions.reset_state()
    yield
    permissions.reset_state()


# ── configuration ──────────────────────────────────────────────

def make_settings(**overrides: Any) -> Settings:
    """A valid non-production ``Settings`` with no file or env input.

    ``app_env="test"`` matters: it downgrades "you are using SQLite" from fatal
    to a warning, which is exactly the local/CI posture.
    """
    values: dict[str, Any] = {
        "bot_token": FAKE_BOT_TOKEN,
        "main_admin_id": MAIN_ADMIN_ID,
        "database_url": TEST_DB_URL,
        "app_env": "test",
        "run_migrations": False,
        "default_coins": "BTC,ETH,SOL",
        "min_whale_value": 2_000_000.0,
        "alert_cooldown_seconds": 30,
        "offline": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def env() -> Settings:
    return make_settings()


# ── database ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def database() -> Any:
    """A fresh in-memory schema per test.

    SQLAlchemy gives ``sqlite+aiosqlite:///:memory:`` a ``StaticPool``, so every
    session reuses the one connection and the schema created by ``create_all()``
    is still there when the test runs.
    """
    db = Database(TEST_DB_URL)
    await db.connect()
    await db.create_all()
    set_database(db)
    try:
        yield db
    finally:
        await db.disconnect()


@pytest_asyncio.fixture
async def container(env: Settings, database: Database) -> Any:
    """A wired application container.

    ``AppContainer.__init__`` performs no I/O — it only constructs services —
    so this is cheap and needs no event loop of its own.
    """
    app = AppContainer(env, database)
    await app.restore()
    try:
        yield app
    finally:
        await app.alerts.stop()


# ── fake Telegram objects ──────────────────────────────────────
#
# Handlers only ever touch: update.effective_user / .effective_chat /
# .effective_message / .callback_query, and context.args / .user_data /
# .bot_data / .bot. Real PTB objects would drag in a live Bot for
# ``query.answer()``, so these duck-typed stand-ins are both smaller and
# stricter — an unexpected attribute access fails loudly.

class FakeUser:
    def __init__(self, user_id: int, username: str | None = None, first_name: str = "Test"):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.is_bot = False


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id
        self.type = "private"
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, text: str, **kwargs: Any) -> SimpleNamespace:
        self.sent.append({"text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent), text=text)

    @property
    def last(self) -> str | None:
        return self.sent[-1]["text"] if self.sent else None


class FakeMessage:
    def __init__(self, chat: FakeChat, text: str | None = None, message_id: int = 1):
        self.chat = chat
        self.text = text
        self.message_id = message_id
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> SimpleNamespace:
        self.replies.append(text)
        return SimpleNamespace(message_id=self.message_id + 1, text=text)


class FakeCallbackQuery:
    def __init__(self, data: str, user: FakeUser, message: FakeMessage):
        self.data = data
        self.from_user = user
        self.message = message
        self.id = "cbq-1"
        self.answers: list[dict[str, Any]] = []
        self.edits: list[str] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **kw: Any) -> None:
        self.answers.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)

    @property
    def alerts(self) -> list[str]:
        """Answers shown as a pop-up — how a refusal reaches the user."""
        return [a["text"] for a in self.answers if a["show_alert"] and a["text"]]


class FakeUpdate:
    """Duck-typed stand-in for ``telegram.Update``."""

    def __init__(
        self,
        user_id: int,
        *,
        text: str | None = None,
        callback_data: str | None = None,
        username: str | None = None,
        chat_id: int | None = None,
    ):
        self.effective_user = FakeUser(user_id, username)
        self.effective_chat = FakeChat(chat_id if chat_id is not None else user_id)
        self.effective_message = FakeMessage(self.effective_chat, text)
        self.callback_query = (
            FakeCallbackQuery(callback_data, self.effective_user, self.effective_message)
            if callback_data is not None
            else None
        )
        self.update_id = 1

    # Convenience accessors used by assertions.
    @property
    def sent(self) -> list[str]:
        """Everything the bot said, whether by message, edit or callback answer."""
        out = [item["text"] for item in self.effective_chat.sent]
        out += self.effective_message.replies
        if self.callback_query is not None:
            out += self.callback_query.edits
            out += [a["text"] for a in self.callback_query.answers if a["text"]]
        return out

    @property
    def last(self) -> str:
        assert self.sent, "the handler produced no user-visible output"
        return self.sent[-1]


class FakeBot:
    """Only ``send_message`` is exercised (by ``AlertService``)."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.menus: list[dict[str, Any]] = []
        self.deleted_menus: list[Any] = []
        self.raises: Exception | None = None
        self._counter = 0

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        if self.raises is not None:
            raise self.raises
        self._counter += 1
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=self._counter)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
        **kwargs: Any,
    ) -> bool:
        """Needed when a test drives a *real* ``telegram.Update``."""
        if self.raises is not None:
            raise self.raises
        self.answers.append({"text": text, "show_alert": show_alert})
        return True

    async def set_my_commands(self, commands: Any = (), **kw: Any) -> bool:
        self.menus.append({"commands": tuple(commands), "scope": kw.get("scope")})
        return True

    async def delete_my_commands(self, **kw: Any) -> bool:
        """A demoted co-admin's chat scope is deleted, not overwritten."""
        self.deleted_menus.append(kw.get("scope"))
        return True


class FakeContext:
    """Duck-typed stand-in for ``ContextTypes.DEFAULT_TYPE``."""

    def __init__(self, app: AppContainer, args: list[str] | None = None):
        self.bot_data: dict[str, Any] = {"app": app}
        self.user_data: dict[str, Any] = {}
        self.chat_data: dict[str, Any] = {}
        self.args = args or []
        self.bot = FakeBot()
        self.error: BaseException | None = None
        self.application = SimpleNamespace(bot_data=self.bot_data)


@pytest.fixture
def ctx(container: AppContainer) -> FakeContext:
    return FakeContext(container)


@pytest.fixture
def main_admin() -> FakeUser:
    return FakeUser(MAIN_ADMIN_ID, "mainadmin")


async def drain(*, times: int = 3) -> None:
    """Yield to the loop so queued background work can run."""
    for _ in range(times):
        await asyncio.sleep(0)
