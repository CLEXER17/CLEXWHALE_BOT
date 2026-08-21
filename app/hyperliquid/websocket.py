"""Hyperliquid websocket client with automatic reconnect and resubscribe.

One instance owns one TCP connection and an arbitrary set of subscriptions.
The engine runs two instances — one for global market feeds (``trades``,
``l2Book``, ``activeAssetCtx``) and one for the per-wallet focus slate
(``orderUpdates``) — which keeps us far inside the documented ceilings of 10
connections and 1000 subscriptions per IP.

Reconnect strategy: exponential backoff with jitter, then replay the desired
subscription set. Subscriptions requested while offline are queued, so callers
never need to know the socket state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Awaitable, Callable

from websockets.exceptions import ConnectionClosed, WebSocketException

try:  # websockets >= 13 (new asyncio implementation)
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - older websockets
    from websockets.client import connect as ws_connect  # type: ignore[no-redef]

from app.hyperliquid.constants import CHANNEL_ALIASES, USER_SUBSCRIPTIONS
from app.utils.backoff import ExponentialBackoff
from app.utils.formatting import utc_now
from app.utils.logging import get_logger

log = get_logger(__name__)

MessageHandler = Callable[[str, Any], Awaitable[None]]
Subscription = dict[str, Any]

APP_PING_INTERVAL = 45.0


def subscription_key(subscription: Subscription) -> str:
    return json.dumps(subscription, sort_keys=True, separators=(",", ":"))


class HyperliquidWebSocket:
    def __init__(
        self,
        url: str,
        handler: MessageHandler,
        name: str = "market",
        max_subscriptions: int = 500,
    ) -> None:
        self.url = url
        self.handler = handler
        self.name = name
        self.max_subscriptions = max_subscriptions

        self._desired: dict[str, Subscription] = {}
        self._active: set[str] = set()
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._backoff = ExponentialBackoff(base=1.0, factor=2.0, maximum=60.0)

        # observability
        self.connected = False
        self.connected_since = None
        self.last_message_at = None
        self.messages_received = 0
        self.messages_sent = 0
        self.reconnects = 0
        self.last_error: str | None = None

    # ── lifecycle ─────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name=f"ws-{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._ping_task, self._task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._ping_task = None
        self._task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        self.connected = False
        self._active.clear()

    # ── subscription management ───────────────────────────────
    @property
    def subscriptions(self) -> list[Subscription]:
        return list(self._desired.values())

    @property
    def unique_users(self) -> set[str]:
        users = set()
        for sub in self._desired.values():
            if sub.get("type") in USER_SUBSCRIPTIONS and sub.get("user"):
                users.add(str(sub["user"]).lower())
        return users

    async def subscribe(self, subscription: Subscription) -> bool:
        key = subscription_key(subscription)
        if key in self._desired:
            return True
        if len(self._desired) >= self.max_subscriptions:
            log.warning(
                "Subscription cap reached, refusing new subscription",
                extra={"ws": self.name, "cap": self.max_subscriptions, "sub": key},
            )
            return False
        self._desired[key] = subscription
        if self.connected:
            await self._send({"method": "subscribe", "subscription": subscription})
            self._active.add(key)
        return True

    async def unsubscribe(self, subscription: Subscription) -> None:
        key = subscription_key(subscription)
        self._desired.pop(key, None)
        if key in self._active:
            self._active.discard(key)
            if self.connected:
                await self._send({"method": "unsubscribe", "subscription": subscription})

    async def replace_subscriptions(self, subscriptions: list[Subscription]) -> None:
        """Diff the desired set against ``subscriptions`` and apply the delta."""
        wanted = {subscription_key(s): s for s in subscriptions}
        for key, sub in list(self._desired.items()):
            if key not in wanted:
                await self.unsubscribe(sub)
        for sub in wanted.values():
            await self.subscribe(sub)

    # ── internals ─────────────────────────────────────────────
    async def _send(self, message: dict[str, Any]) -> bool:
        if self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps(message))
            self.messages_sent += 1
            return True
        except (ConnectionClosed, WebSocketException, RuntimeError) as exc:
            log.warning("Websocket send failed", extra={"ws": self.name, "error": str(exc)})
            return False

    async def _resubscribe(self) -> None:
        self._active.clear()
        for key, sub in list(self._desired.items()):
            if await self._send({"method": "subscribe", "subscription": sub}):
                self._active.add(key)
                # Stay well under 2000 outbound messages/minute on large sets.
                await asyncio.sleep(0.02)

    async def _ping_loop(self) -> None:
        """Hyperliquid drops idle sockets; an application ping keeps it warm."""
        while not self._stopping:
            await asyncio.sleep(APP_PING_INTERVAL)
            if self.connected:
                await self._send({"method": "ping"})

    async def _run(self) -> None:
        while not self._stopping:
            try:
                log.info("Connecting websocket", extra={"ws": self.name, "url": self.url})
                async with ws_connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=1024,
                ) as socket:
                    self._ws = socket
                    self.connected = True
                    self.connected_since = utc_now()
                    self._backoff.reset()
                    log.info(
                        "Hyperliquid WebSocket connected",
                        extra={"ws": self.name, "subscriptions": len(self._desired)},
                    )
                    await self._resubscribe()
                    if self._ping_task is None or self._ping_task.done():
                        self._ping_task = asyncio.create_task(
                            self._ping_loop(), name=f"ws-ping-{self.name}"
                        )
                    async for raw in socket:
                        self.messages_received += 1
                        self.last_message_at = utc_now()
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "Hyperliquid WebSocket disconnected",
                    extra={"ws": self.name, "error": self.last_error},
                )
            except Exception as exc:  # pragma: no cover - defensive
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Websocket loop crashed", extra={"ws": self.name})
            finally:
                self.connected = False
                self._ws = None
                self._active.clear()

            if self._stopping:
                break
            self.reconnects += 1
            delay = self._backoff.next_delay()
            log.info("Reconnecting...", extra={"ws": self.name, "in_seconds": round(delay, 2)})
            await asyncio.sleep(delay)

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("Dropping non-JSON websocket frame", extra={"ws": self.name})
            return
        if not isinstance(message, dict):
            return
        channel = message.get("channel")
        if not isinstance(channel, str):
            return
        if channel in {"subscriptionResponse", "pong"}:
            return
        if channel == "error":
            log.warning("Websocket error frame", extra={"ws": self.name, "detail": str(message.get("data"))[:300]})
            return
        channel = CHANNEL_ALIASES.get(channel, channel)
        try:
            await self.handler(channel, message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception:  # one bad event must not kill the socket
            log.exception("Handler error", extra={"ws": self.name, "channel": channel})

    # ── health ────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "subscriptions": len(self._desired),
            "active": len(self._active),
            "unique_users": len(self.unique_users),
            "messages_received": self.messages_received,
            "messages_sent": self.messages_sent,
            "reconnects": self.reconnects,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "last_error": self.last_error,
        }
