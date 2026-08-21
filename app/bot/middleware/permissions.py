"""Authorization and reply plumbing for every Telegram handler.

Three jobs:

1. **Authorize.** The caller's identity is always taken from
   ``update.effective_user.id``. Callback data is never trusted as proof of who
   is asking: a user who types ``adm:add`` into a crafted inline button reaches
   :func:`requires` exactly like anyone else and is refused, because the role is
   re-derived from the Telegram user id and checked against the capability. The
   same decorator guards commands and callbacks, so there is no second, weaker
   path into an admin action.

2. **Throttle.** A per-user token bucket keeps one account from flooding the
   bot; the refusal is a visible message, never a silent drop.

3. **Reply.** ``respond`` hides the difference between "answer a command" and
   "edit the message the button lives on", so a view can be reached from either
   without duplicating code (spec §27: every setting reachable by button *and*
   command).
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from app.bot.messages import texts
from app.container import AppContainer
from app.services.admin_service import Actor, AdminError, Capability
from app.utils.logging import get_logger
from app.utils.ratelimit import TokenBucket

log = get_logger(__name__)

#: 1 action/second sustained, bursts of 10. Generous for a human, useless for a
#: script trying to brute-force callback data.
_COMMAND_BUCKET = TokenBucket(rate=1.0, capacity=10.0)

#: Re-register a user in the database at most this often (seconds).
_REGISTER_TTL = 300.0
_registered: dict[int, float] = {}

T = TypeVar("T")
ActorHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE, Actor], Awaitable[Any]]
PlainHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]


def reset_state() -> None:
    """Clear process-level caches. Used by the test-suite."""
    _COMMAND_BUCKET._buckets.clear()
    _registered.clear()


# ── container access ───────────────────────────────────────────
def get_container(context: ContextTypes.DEFAULT_TYPE) -> AppContainer:
    app = context.bot_data.get("app")
    if app is None:  # pragma: no cover - wiring bug, not a runtime condition
        raise RuntimeError("AppContainer was not installed in bot_data['app']")
    return app


# ── replying ───────────────────────────────────────────────────
async def respond(
    update: Update,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    edit: bool | None = None,
    alert: str | None = None,
    toast: str | None = None,
) -> None:
    """Send ``text`` back through whichever channel the update arrived on.

    For a callback query the message is edited in place (so panels feel like a
    UI rather than a chat log); for a command a new message is sent. ``edit``
    forces either behaviour. ``toast``/``alert`` add a short confirmation on top
    of the edit, which is how a button press acknowledges itself.
    """
    query = update.callback_query
    if query is not None:
        try:
            await query.answer(
                plain_text(alert or toast or "")[:190] or None, show_alert=bool(alert)
            )
        except TelegramError:
            # An expired query id is not worth failing the handler over.
            pass

    should_edit = (query is not None) if edit is None else edit
    if should_edit and query is not None and query.message is not None:
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return  # Identical content — the refresh simply had nothing to do.
            log.debug("edit_message_text failed, sending instead", extra={"error": str(exc)})

    chat = update.effective_chat
    if chat is None:  # pragma: no cover
        return
    await chat.send_message(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def notify(update: Update, text: str, *, alert: bool = False) -> None:
    """Short confirmation: a toast for callbacks, a message for commands."""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer(text[:190], show_alert=alert)
            return
        except TelegramError:
            pass
    chat = update.effective_chat
    if chat is not None:
        await chat.send_message(text, parse_mode=ParseMode.HTML)


# ── bookkeeping ────────────────────────────────────────────────
async def register(update: Update, container: AppContainer) -> None:
    """Record the user so alerts can be delivered to them later."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    now = time.monotonic()
    last = _registered.get(user.id)
    if last is not None and now - last < _REGISTER_TTL:
        return
    _registered[user.id] = now
    await container.admins.register_user(
        telegram_id=user.id,
        chat_id=chat.id,
        username=user.username,
        first_name=user.first_name,
    )


# ── the guard ──────────────────────────────────────────────────
def requires(
    capability: Capability,
    *,
    register_user: bool = True,
    allow_when_paused: bool = False,
) -> Callable[[ActorHandler], PlainHandler]:
    """Wrap a handler so it only runs for a caller holding ``capability``.

    The wrapped function is called as ``handler(update, context, actor)``. It can
    assume the actor is authorized; it must not re-derive identity from callback
    data or command arguments.

    ``allow_when_paused`` marks the few handlers that must still work while the
    bot is globally paused — /go above all, plus the read-only status views that
    tell an admin *why* nothing is happening. Everything else is refused with a
    single message, enforced here so no handler can forget the pause.
    """

    def decorator(func: ActorHandler) -> PlainHandler:
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
            container = get_container(context)
            user = update.effective_user
            telegram_id = user.id if user is not None else None

            if telegram_id is None:  # channel post, service message, ...
                return None

            if not throttle(telegram_id):
                log.info("Throttled Telegram caller", extra={"telegram_id": telegram_id})
                await notify(update, texts.rate_limited(), alert=True)
                return None

            public_mode = container.settings.config.public_mode
            try:
                actor = container.admins.require(
                    telegram_id, capability, public_mode=public_mode
                )
            except AdminError as exc:
                await refuse(update, str(exc))
                return None

            # The pause is checked after authorization so an unauthorized caller
            # learns nothing about the bot's state from the refusal it gets.
            if container.settings.config.paused and not allow_when_paused:
                await refuse(update, texts.paused_notice(admin=actor.is_admin))
                return None

            if register_user:
                await register(update, container)

            return await func(update, context, actor)

        return wrapper

    return decorator


def throttle(telegram_id: int) -> bool:
    """False when the caller has exceeded their command budget."""
    return _COMMAND_BUCKET.consume(telegram_id)


async def refuse(update: Update, message: str) -> None:
    """Tell the caller no. Callbacks get an alert; commands get a message."""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer(plain_text(message)[:190], show_alert=True)
            return
        except TelegramError:
            pass
    chat = update.effective_chat
    if chat is not None:
        await chat.send_message(message, parse_mode=ParseMode.HTML)


def plain_text(text: str) -> str:
    """Strip the HTML tags Telegram will not render inside a callback alert."""
    out: list[str] = []
    depth = 0
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out)


def actor_of(update: Update, container: AppContainer) -> Actor:
    """Resolve the caller without enforcing a capability (for /start, /help)."""
    user = update.effective_user
    if user is None:
        return container.admins.actor(None)
    return container.admins.actor(user.id, user.username)
