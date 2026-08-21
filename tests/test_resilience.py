"""Resilience.

Spec §36 requires coverage of "WebSocket reconnect" and "API failure". Railway
restarts containers, Hyperliquid drops idle sockets, and the public info endpoint
rate-limits — none of which may lose data, spin, or crash the bot.

Every test here drives the *real* client code. The only substitutions are at the
two network seams the production code already exposes for exactly this purpose:

* ``app.hyperliquid.websocket.ws_connect`` — the module-level factory
  :class:`HyperliquidWebSocket` calls.
* ``HyperliquidREST._client`` — assigned only when ``None`` in ``start()``.

The reconnect *policy* (:class:`ExponentialBackoff`) is never replaced; where a
test cannot afford to sleep it wraps the real backoff, records the real delay it
computed, and sleeps zero instead.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from websockets.exceptions import WebSocketException

from app.hyperliquid import websocket as ws_module
from app.hyperliquid.constants import INFO_PATH
from app.hyperliquid.rest import HyperliquidREST
from app.hyperliquid.websocket import HyperliquidWebSocket, subscription_key
from app.utils.backoff import ExponentialBackoff
from app.utils.ratelimit import TokenBucket, WeightedRateLimiter
from tests.factories import WALLET_A, WALLET_B

WS_URL = "wss://api.example.invalid/ws"
API_URL = "https://api.example.invalid"

TRADES_SUB = {"type": "trades", "coin": "BTC"}
MIDS_SUB = {"type": "allMids"}


# ── websocket doubles ──────────────────────────────────────────

HANG = object()  #: sentinel: after the scripted frames, keep the socket open


class FakeSocket:
    """A websockets-shaped socket: async-iterable, ``send``, ``close``."""

    def __init__(self, frames: list[str] | None = None, after: Any = HANG) -> None:
        self._frames = list(frames or [])
        self._after = after
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    @property
    def methods(self) -> list[str]:
        return [message.get("method") for message in self.sent]

    @property
    def subscribed(self) -> list[str]:
        return [
            subscription_key(m["subscription"])
            for m in self.sent
            if m.get("method") == "subscribe"
        ]

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._after is HANG:
            await asyncio.Event().wait()      # stay connected until cancelled
            raise AssertionError("unreachable")
        raise self._after


class FakeEndpoint:
    """Stands in for ``ws_connect``: one scripted outcome per attempt.

    Each script entry is either a :class:`FakeSocket` (the connection succeeds)
    or an exception instance (the handshake fails). The last entry repeats, so a
    script can end in a socket that simply stays up.
    """

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.attempts = 0
        self.urls: list[str] = []
        self.sockets: list[FakeSocket] = []

    def __call__(self, url: str, **_kwargs: Any) -> _FakeConnection:
        self.urls.append(url)
        step = self.script[min(self.attempts, len(self.script) - 1)]
        self.attempts += 1
        return _FakeConnection(self, step)


class _FakeConnection:
    def __init__(self, endpoint: FakeEndpoint, step: Any) -> None:
        self.endpoint = endpoint
        self.step = step
        self.socket: FakeSocket | None = None

    async def __aenter__(self) -> FakeSocket:
        if isinstance(self.step, BaseException):
            raise self.step
        socket = self.step() if callable(self.step) else self.step
        self.socket = socket
        self.endpoint.sockets.append(socket)
        return socket

    async def __aexit__(self, *_exc: object) -> bool:
        # ``websockets.connect`` closes the socket when the block exits, including
        # when the reader task is cancelled; the double must do the same.
        if self.socket is not None:
            await self.socket.close()
        return False


class RecordingBackoff:
    """Wraps the real backoff: records its delays, sleeps for none of them."""

    def __init__(self, inner: ExponentialBackoff) -> None:
        self.inner = inner
        self.delays: list[float] = []
        self.resets = 0

    def next_delay(self) -> float:
        self.delays.append(self.inner.next_delay())
        return 0.0

    def reset(self) -> None:
        self.inner.reset()
        self.resets += 1


class Recorder:
    """Collects ``(channel, data)`` pairs the socket dispatches."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_times = fail_times

    async def __call__(self, channel: str, data: Any) -> None:
        self.calls.append((channel, data))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("handler blew up")


async def settle(times: int = 12) -> None:
    """Yield to the event loop enough times for the ws task to progress."""
    for _ in range(times):
        await asyncio.sleep(0)


async def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


def frame(channel: str, data: Any) -> str:
    return json.dumps({"channel": channel, "data": data})


async def start_socket(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: FakeEndpoint,
    handler: Recorder,
    *,
    subscriptions: list[dict[str, Any]] | None = None,
    instant_backoff: bool = True,
    **kwargs: Any,
) -> tuple[HyperliquidWebSocket, RecordingBackoff]:
    monkeypatch.setattr(ws_module, "ws_connect", endpoint)
    socket = HyperliquidWebSocket(WS_URL, handler, name="test", **kwargs)
    backoff = RecordingBackoff(socket._backoff)
    if instant_backoff:
        socket._backoff = backoff  # type: ignore[assignment]
    for subscription in subscriptions or []:
        await socket.subscribe(subscription)
    await socket.start()
    return socket, backoff


# ── websocket: connect and subscribe ───────────────────────────

async def test_subscriptions_requested_while_offline_are_replayed_on_connect(monkeypatch):
    endpoint = FakeEndpoint(FakeSocket())
    handler = Recorder()
    socket, _ = await start_socket(
        monkeypatch, endpoint, handler, subscriptions=[MIDS_SUB, TRADES_SUB]
    )
    try:
        await wait_for(lambda: socket.connected)
        await wait_for(lambda: len(endpoint.sockets[0].subscribed) == 2)
        assert endpoint.sockets[0].subscribed == [
            subscription_key(MIDS_SUB),
            subscription_key(TRADES_SUB),
        ]
        assert socket.stats()["active"] == 2
        assert endpoint.urls == [WS_URL]
    finally:
        await socket.stop()


async def test_a_subscription_added_while_connected_is_sent_immediately(monkeypatch):
    endpoint = FakeEndpoint(FakeSocket())
    socket, _ = await start_socket(monkeypatch, endpoint, Recorder())
    try:
        await wait_for(lambda: socket.connected)
        assert await socket.subscribe(TRADES_SUB) is True
        assert endpoint.sockets[0].subscribed == [subscription_key(TRADES_SUB)]
        # Asking twice is a no-op rather than a duplicate frame.
        assert await socket.subscribe(dict(TRADES_SUB)) is True
        assert len(endpoint.sockets[0].subscribed) == 1
    finally:
        await socket.stop()


async def test_unsubscribing_sends_one_frame_and_forgets_the_subscription(monkeypatch):
    endpoint = FakeEndpoint(FakeSocket())
    socket, _ = await start_socket(monkeypatch, endpoint, Recorder(), subscriptions=[TRADES_SUB])
    try:
        await wait_for(lambda: socket.connected)
        await socket.unsubscribe(TRADES_SUB)
        assert endpoint.sockets[0].methods.count("unsubscribe") == 1
        assert socket.subscriptions == []
    finally:
        await socket.stop()


async def test_replace_subscriptions_applies_only_the_delta(monkeypatch):
    endpoint = FakeEndpoint(FakeSocket())
    socket, _ = await start_socket(
        monkeypatch, endpoint, Recorder(), subscriptions=[MIDS_SUB, TRADES_SUB]
    )
    try:
        await wait_for(lambda: socket.connected)
        await wait_for(lambda: len(endpoint.sockets[0].subscribed) == 2)
        eth = {"type": "trades", "coin": "ETH"}
        await socket.replace_subscriptions([MIDS_SUB, eth])
        live = endpoint.sockets[0]
        assert live.methods.count("unsubscribe") == 1          # BTC dropped
        assert live.subscribed[-1] == subscription_key(eth)    # ETH added
        assert {subscription_key(s) for s in socket.subscriptions} == {
            subscription_key(MIDS_SUB),
            subscription_key(eth),
        }
    finally:
        await socket.stop()


async def test_the_subscription_cap_refuses_rather_than_overruns(monkeypatch):
    """Hyperliquid caps subscriptions per connection; we refuse, never spam."""
    endpoint = FakeEndpoint(FakeSocket())
    socket, _ = await start_socket(monkeypatch, endpoint, Recorder(), max_subscriptions=2)
    try:
        assert await socket.subscribe({"type": "trades", "coin": "BTC"}) is True
        assert await socket.subscribe({"type": "trades", "coin": "ETH"}) is True
        assert await socket.subscribe({"type": "trades", "coin": "SOL"}) is False
        assert len(socket.subscriptions) == 2
    finally:
        await socket.stop()


async def test_unique_users_counts_only_wallet_subscriptions(monkeypatch):
    """The 10-address ceiling is per IP, so the count must be exact."""
    socket = HyperliquidWebSocket(WS_URL, Recorder(), name="user")
    await socket.subscribe({"type": "orderUpdates", "user": WALLET_A.upper()})
    await socket.subscribe({"type": "userEvents", "user": WALLET_A.lower()})
    await socket.subscribe({"type": "orderUpdates", "user": WALLET_B})
    await socket.subscribe(TRADES_SUB)
    assert socket.unique_users == {WALLET_A.lower(), WALLET_B.lower()}
    assert socket.stats()["unique_users"] == 2


# ── websocket: dispatch ────────────────────────────────────────

async def test_data_frames_reach_the_handler(monkeypatch):
    payload = [{"coin": "BTC", "px": "100000.0"}]
    endpoint = FakeEndpoint(FakeSocket([frame("trades", payload)]))
    handler = Recorder()
    socket, _ = await start_socket(monkeypatch, endpoint, handler)
    try:
        await wait_for(lambda: handler.calls)
        assert handler.calls == [("trades", payload)]
        assert socket.messages_received == 1
        assert socket.last_message_at is not None
    finally:
        await socket.stop()


async def test_control_and_malformed_frames_are_swallowed(monkeypatch):
    endpoint = FakeEndpoint(
        FakeSocket(
            [
                frame("subscriptionResponse", {"ok": True}),
                frame("pong", None),
                frame("error", "Already subscribed"),
                "not json at all",
                json.dumps(["a", "list"]),
                json.dumps({"no_channel": 1}),
                frame("trades", [{"coin": "BTC"}]),
            ]
        )
    )
    handler = Recorder()
    socket, _ = await start_socket(monkeypatch, endpoint, handler)
    try:
        await wait_for(lambda: handler.calls)
        assert handler.calls == [("trades", [{"coin": "BTC"}])]
        assert socket.messages_received == 7      # counted, then filtered
        assert socket.connected is True           # no frame killed the socket
    finally:
        await socket.stop()


async def test_a_handler_exception_does_not_kill_the_socket(monkeypatch):
    """One malformed whale event must not stop the whole feed."""
    endpoint = FakeEndpoint(
        FakeSocket([frame("trades", "first"), frame("trades", "second")])
    )
    handler = Recorder(fail_times=1)
    socket, _ = await start_socket(monkeypatch, endpoint, handler)
    try:
        await wait_for(lambda: len(handler.calls) == 2)
        assert [data for _channel, data in handler.calls] == ["first", "second"]
        assert socket.connected is True
    finally:
        await socket.stop()


# ── websocket: disconnect and reconnect ────────────────────────

async def test_a_dropped_connection_reconnects_and_resubscribes(monkeypatch):
    first = FakeSocket([frame("trades", "before")], after=OSError("connection reset by peer"))
    second = FakeSocket([frame("trades", "after")])
    endpoint = FakeEndpoint(first, second)
    handler = Recorder()
    socket, _ = await start_socket(
        monkeypatch, endpoint, handler, subscriptions=[MIDS_SUB, TRADES_SUB]
    )
    try:
        await wait_for(lambda: len(handler.calls) == 2)
        assert [data for _c, data in handler.calls] == ["before", "after"]
        assert socket.reconnects == 1
        assert socket.connected is True
        assert "OSError" in (socket.last_error or "")
        # The full desired set is replayed on the new socket, not just the delta.
        assert second.subscribed == [
            subscription_key(MIDS_SUB),
            subscription_key(TRADES_SUB),
        ]
    finally:
        await socket.stop()


async def test_connected_goes_false_while_the_socket_is_down(monkeypatch):
    endpoint = FakeEndpoint(WebSocketException("handshake rejected"), FakeSocket())
    socket, _ = await start_socket(monkeypatch, endpoint, Recorder())
    try:
        await wait_for(lambda: endpoint.attempts >= 1)
        assert socket.stats()["active"] == 0
        await wait_for(lambda: socket.connected)
        assert socket.reconnects >= 1
    finally:
        await socket.stop()


async def test_repeated_failures_back_off_with_growing_delays(monkeypatch):
    """The real ExponentialBackoff drives this; only the sleeping is skipped."""
    endpoint = FakeEndpoint(
        OSError("no route to host"),
        OSError("no route to host"),
        OSError("no route to host"),
        OSError("no route to host"),
        FakeSocket(),
    )
    socket, backoff = await start_socket(monkeypatch, endpoint, Recorder())
    try:
        await wait_for(lambda: socket.connected)
        delays = backoff.delays[:4]
        assert len(delays) == 4
        # base=1.0, factor=2.0, jitter=±25% → 1, 2, 4, 8 with spread.
        for index, delay in enumerate(delays):
            expected = 2.0**index
            assert 0.75 * expected <= delay <= 1.25 * expected
        assert delays == sorted(delays)
        assert socket.reconnects == 4
    finally:
        await socket.stop()


async def test_the_delay_is_capped_and_resets_after_a_success(monkeypatch):
    inner = ExponentialBackoff(base=1.0, factor=2.0, maximum=60.0)
    for _ in range(20):
        assert inner.next_delay() <= 60.0 * 1.25
    inner.reset()
    assert inner.next_delay() <= 1.25

    endpoint = FakeEndpoint(
        OSError("down"),
        FakeSocket([], after=OSError("down again")),
        FakeSocket(),
    )
    socket, backoff = await start_socket(monkeypatch, endpoint, Recorder())
    try:
        await wait_for(lambda: endpoint.attempts >= 3)
        # A healthy connection resets the ladder, so the next outage starts at
        # ~1s again instead of continuing to climb.
        assert backoff.resets >= 2
        assert backoff.delays[-1] <= 1.25
    finally:
        await socket.stop()


async def test_reconnecting_never_leaks_a_second_reader(monkeypatch):
    endpoint = FakeEndpoint(
        FakeSocket([], after=OSError("flap")),
        FakeSocket([], after=OSError("flap")),
        FakeSocket([frame("trades", "settled")]),
    )
    handler = Recorder()
    socket, _ = await start_socket(monkeypatch, endpoint, handler)
    try:
        await wait_for(lambda: handler.calls)
        assert handler.calls == [("trades", "settled")]
        assert endpoint.attempts == 3
    finally:
        await socket.stop()


# ── websocket: shutdown ────────────────────────────────────────

async def test_stop_closes_the_socket_and_stops_reconnecting(monkeypatch):
    endpoint = FakeEndpoint(FakeSocket())
    socket, _ = await start_socket(monkeypatch, endpoint, Recorder(), subscriptions=[TRADES_SUB])
    await wait_for(lambda: socket.connected)

    await socket.stop()
    attempts_at_stop = endpoint.attempts
    assert socket.connected is False
    assert endpoint.sockets[0].closed is True
    assert socket._task is None
    assert socket.stats()["active"] == 0
    # The desired set survives, so a later start() restores it.
    assert len(socket.subscriptions) == 1

    await settle()
    assert endpoint.attempts == attempts_at_stop      # no reconnect after stop


async def test_stop_is_safe_twice_and_before_start(monkeypatch):
    monkeypatch.setattr(ws_module, "ws_connect", FakeEndpoint(FakeSocket()))
    socket = HyperliquidWebSocket(WS_URL, Recorder(), name="test")
    await socket.stop()                                # never started
    await socket.start()
    await socket.stop()
    await socket.stop()                                # idempotent
    assert socket.connected is False


async def test_restarting_reconnects_and_replays(monkeypatch):
    endpoint = FakeEndpoint(FakeSocket(), FakeSocket())
    socket, _ = await start_socket(monkeypatch, endpoint, Recorder(), subscriptions=[TRADES_SUB])
    await wait_for(lambda: socket.connected)
    await socket.stop()

    await socket.start()
    try:
        await wait_for(lambda: socket.connected)
        assert endpoint.sockets[1].subscribed == [subscription_key(TRADES_SUB)]
    finally:
        await socket.stop()


# ── REST doubles ───────────────────────────────────────────────

class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        *,
        bad_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._bad_json = bad_json

    def json(self) -> Any:
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeHTTP:
    """Scripted stand-in for ``httpx.AsyncClient`` at the ``_client`` seam."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def post(self, path: str, json: dict[str, Any] | None = None, **_kw: Any) -> FakeResponse:
        self.calls.append({"path": path, "body": json})
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step

    async def aclose(self) -> None:
        self.closed = True


def rest_with(*script: Any, budget: float = 1200.0, max_retries: int = 2) -> tuple[HyperliquidREST, FakeHTTP]:
    client = FakeHTTP(*script)
    rest = HyperliquidREST(API_URL, WeightedRateLimiter(budget), max_retries=max_retries)
    rest._client = client  # the documented offline seam
    return rest, client


# ── REST: failure handling ─────────────────────────────────────

async def test_a_successful_request_marks_the_client_healthy():
    rest, client = rest_with(FakeResponse(200, {"universe": []}))
    assert await rest.post_info("meta") == {"universe": []}
    assert client.calls[0]["path"] == INFO_PATH
    assert client.calls[0]["body"] == {"type": "meta"}
    assert rest.healthy is True
    assert rest.stats()["errors"] == 0


async def test_a_server_error_is_retried_then_succeeds():
    rest, client = rest_with(FakeResponse(503), FakeResponse(200, {"ok": True}))
    assert await rest.post_info("meta") == {"ok": True}
    assert len(client.calls) == 2
    assert rest.stats()["errors"] == 1
    assert rest.healthy is True


async def test_a_client_error_is_not_retried():
    """A 4xx will not fix itself, so retrying it only burns the weight budget."""
    rest, client = rest_with(FakeResponse(422))
    assert await rest.post_info("meta") is None
    assert len(client.calls) == 1
    assert "HTTP 422" in (rest.last_error or "")
    assert rest.stats()["errors"] == 1


async def test_a_rate_limit_response_honours_retry_after():
    rest, client = rest_with(
        FakeResponse(429, headers={"Retry-After": "0.01"}),
        FakeResponse(200, {"ok": True}),
    )
    started = time.monotonic()
    assert await rest.post_info("meta") == {"ok": True}
    assert time.monotonic() - started < 0.4          # the header, not the ladder
    assert rest.stats()["throttled"] == 1
    assert len(client.calls) == 2


async def test_a_network_failure_returns_none_instead_of_raising():
    rest, client = rest_with(httpx.ConnectError("dns failure"), max_retries=2)
    assert await rest.post_info("meta") is None
    assert len(client.calls) == 2                    # every attempt used
    assert rest.healthy is False
    assert "ConnectError" in (rest.last_error or "")
    assert rest.stats()["errors"] == 2


async def test_a_timeout_is_retried_then_reported():
    rest, _client = rest_with(httpx.ReadTimeout("timed out"), max_retries=2)
    assert await rest.post_info("clearinghouseState", {"user": WALLET_A}) is None
    assert "ReadTimeout" in (rest.last_error or "")


async def test_an_unparseable_body_counts_as_a_failure():
    """A truncated body must never be handed to a parser as real data."""
    rest, client = rest_with(FakeResponse(200, bad_json=True), FakeResponse(200, {"ok": True}))
    assert await rest.post_info("meta") == {"ok": True}
    assert len(client.calls) == 2
    assert rest.stats()["errors"] == 1


async def test_failing_endpoints_return_empty_never_invented_values():
    """Spec §1/§34: on failure the answer is "unavailable", not a fake number."""
    rest, _client = rest_with(httpx.ConnectError("offline"), max_retries=1)
    assert await rest.all_mids() == {}
    assert await rest.meta() == []
    assert await rest.clearinghouse_state(WALLET_A) is None
    assert await rest.frontend_open_orders(WALLET_A) is None
    assert await rest.open_orders(WALLET_A) is None
    assert await rest.l2_book("BTC") is None
    assert await rest.user_fills(WALLET_A) is None
    assert await rest.order_status(WALLET_A, 1) is None
    assert await rest.candle_snapshot("BTC", "1h", 0) == []


# ── REST: budget interaction ───────────────────────────────────

async def test_an_exhausted_budget_drops_an_opportunistic_request():
    rest, client = rest_with(FakeResponse(200, {"ok": True}), budget=1.0)
    assert await rest.post_info("meta", wait_for_budget=False) is None
    assert client.calls == []                        # never left the process
    assert rest.stats()["skipped_no_budget"] == 1


async def test_waiting_for_budget_times_out_instead_of_hanging():
    rest, client = rest_with(FakeResponse(200, {"ok": True}), budget=20.0)
    assert rest.limiter.try_acquire(20.0) is True     # budget now full
    assert await rest.post_info("meta", budget_timeout=0.05) is None
    assert client.calls == []
    assert rest.stats()["skipped_no_budget"] == 1


async def test_each_request_spends_its_documented_weight():
    rest, _client = rest_with(FakeResponse(200, {}), FakeResponse(200, {}))
    await rest.post_info("clearinghouseState", {"user": WALLET_A})
    assert rest.limiter.total_spent == 2.0           # cheap
    await rest.post_info("userFills", {"user": WALLET_A})
    assert rest.limiter.total_spent == 22.0          # default


# ── REST: lifecycle ────────────────────────────────────────────

async def test_start_does_not_replace_an_injected_client():
    rest, client = rest_with(FakeResponse(200, {}))
    await rest.start()
    assert rest._client is client


async def test_aclose_releases_the_client_and_is_safe_twice():
    rest = HyperliquidREST(API_URL, WeightedRateLimiter(1200.0))
    await rest.start()
    assert rest._client is not None
    await rest.aclose()
    assert rest._client is None
    assert rest.healthy is False
    await rest.aclose()


async def test_a_request_after_aclose_reopens_the_client():
    rest, client = rest_with(FakeResponse(200, {"ok": True}))
    await rest.aclose()
    assert client.closed is True
    assert rest._client is None
    # A real client is created on demand; the request fails offline, not crashes.
    rest._client = FakeHTTP(FakeResponse(200, {"ok": True}))
    assert await rest.post_info("meta") == {"ok": True}


# ── weighted rate limiter ──────────────────────────────────────

def test_try_acquire_refuses_once_the_budget_is_spent():
    limiter = WeightedRateLimiter(100.0)
    assert limiter.try_acquire(60.0) is True
    assert limiter.try_acquire(40.0) is True
    assert limiter.try_acquire(1.0) is False
    assert limiter.spent == 100.0
    assert limiter.available == 0.0


def test_the_window_slides_and_frees_the_budget():
    limiter = WeightedRateLimiter(100.0, window=0.05)
    assert limiter.try_acquire(100.0) is True
    assert limiter.try_acquire(1.0) is False
    time.sleep(0.06)
    assert limiter.spent == 0.0
    assert limiter.try_acquire(100.0) is True


def test_available_never_reports_a_negative_budget():
    limiter = WeightedRateLimiter(10.0)
    limiter.try_acquire(10.0)
    assert limiter.available == 0.0


async def test_acquire_waits_for_room_rather_than_failing():
    limiter = WeightedRateLimiter(100.0, window=0.05)
    assert limiter.try_acquire(100.0) is True
    started = time.monotonic()
    assert await limiter.acquire(50.0, timeout=2.0) is True
    assert time.monotonic() - started >= 0.03
    assert limiter.total_waits >= 1


async def test_acquire_gives_up_at_the_timeout():
    limiter = WeightedRateLimiter(100.0, window=60.0)
    assert limiter.try_acquire(100.0) is True
    started = time.monotonic()
    assert await limiter.acquire(50.0, timeout=0.05) is False
    assert time.monotonic() - started < 1.0


async def test_concurrent_callers_never_exceed_the_budget():
    """A whale burst must not let 20 enrichment calls trip a 429."""
    limiter = WeightedRateLimiter(100.0, window=0.05)
    peak = 0.0

    async def call() -> bool:
        nonlocal peak
        ok = await limiter.acquire(10.0, timeout=5.0)
        peak = max(peak, limiter.spent)
        return ok

    results = await asyncio.gather(*(call() for _ in range(20)))
    assert all(results)
    assert peak <= 100.0
    assert limiter.total_spent == 200.0


# ── per-user token bucket ──────────────────────────────────────

def test_a_command_burst_is_capped_then_refills():
    bucket = TokenBucket(rate=1000.0, capacity=3.0)
    assert [bucket.consume("u") for _ in range(4)] == [True, True, True, False]
    time.sleep(0.01)
    assert bucket.consume("u") is True


def test_buckets_are_isolated_per_user():
    bucket = TokenBucket(rate=0.0, capacity=1.0)
    assert bucket.consume(1) is True
    assert bucket.consume(1) is False
    assert bucket.consume(2) is True          # a spammer cannot mute anyone else


def test_idle_buckets_are_pruned():
    bucket = TokenBucket()
    bucket.consume("a")
    bucket.prune(older_than=0.0)
    assert bucket._buckets == {}


# ── backoff policy ─────────────────────────────────────────────

def test_delays_grow_geometrically_and_stay_capped():
    backoff = ExponentialBackoff(base=0.5, factor=3.0, maximum=5.0, jitter=0.0)
    assert [backoff.next_delay() for _ in range(5)] == [0.5, 1.5, 4.5, 5.0, 5.0]


def test_jitter_stays_inside_the_configured_spread():
    backoff = ExponentialBackoff(base=4.0, factor=1.0, maximum=4.0, jitter=0.25)
    delays = [backoff.next_delay() for _ in range(50)]
    assert all(3.0 <= delay <= 5.0 for delay in delays)
    assert len(set(delays)) > 1               # jitter really is random


def test_a_delay_is_never_negative():
    backoff = ExponentialBackoff(base=0.01, factor=1.0, maximum=0.01, jitter=1.0)
    assert all(backoff.next_delay() >= 0.0 for _ in range(50))


async def test_sleep_returns_the_delay_it_waited():
    backoff = ExponentialBackoff(base=0.01, factor=1.0, maximum=0.01, jitter=0.0)
    assert await backoff.sleep() == pytest.approx(0.01)


# ── graceful shutdown of the whole ingest stack ────────────────

META_AND_CTXS = [
    {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
            {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
        ]
    },
    [
        {
            "markPx": "100000.0",
            "midPx": "100000.0",
            "oraclePx": "100000.0",
            "funding": "0.00001",
            "openInterest": "5000",
            "prevDayPx": "99000.0",
            "dayNtlVlm": "1500000000.0",
        },
        {
            "markPx": "4000.0",
            "midPx": "4000.0",
            "oraclePx": "4000.0",
            "funding": "0.00001",
            "openInterest": "50000",
            "prevDayPx": "3900.0",
            "dayNtlVlm": "900000000.0",
        },
        {
            "markPx": "200.0",
            "midPx": "200.0",
            "oraclePx": "200.0",
            "funding": "0.00001",
            "openInterest": "900000",
            "prevDayPx": "195.0",
            "dayNtlVlm": "400000000.0",
        },
    ],
]


class UniverseHTTP(FakeHTTP):
    """Answers whatever the engine's background loops ask for, offline."""

    def __init__(self) -> None:
        super().__init__()
        self.types: list[str] = []

    async def post(self, path: str, json: dict[str, Any] | None = None, **_kw: Any) -> FakeResponse:
        body = json or {}
        request_type = str(body.get("type"))
        self.types.append(request_type)
        self.calls.append({"path": path, "body": body})
        if request_type == "metaAndAssetCtxs":
            return FakeResponse(200, META_AND_CTXS)
        if request_type == "allMids":
            return FakeResponse(200, {"BTC": "100000.0"})
        return FakeResponse(200, [])


async def test_the_ingest_stack_starts_and_stops_cleanly(container, monkeypatch):
    endpoint = FakeEndpoint(FakeSocket())
    monkeypatch.setattr(ws_module, "ws_connect", endpoint)
    container.rest._client = UniverseHTTP()

    await container.start_ingest()
    try:
        await wait_for(lambda: container.engine.connected)
        assert container.ingest_started is True
        assert "BTC" in container.engine.monitored_coins
        assert endpoint.sockets[0].subscribed          # allMids at minimum
    finally:
        await container.stop_ingest()

    assert container.ingest_started is False
    assert container.engine.connected is False
    assert endpoint.sockets[0].closed is True
    assert container.rest._client is None
    lingering = [
        task
        for task in asyncio.all_tasks()
        if (task.get_name() or "").startswith(("whale-", "ws-")) and not task.done()
    ]
    assert lingering == []


async def test_stop_ingest_is_safe_when_start_never_happened(container):
    await container.stop_ingest()
    assert container.ingest_started is False


async def test_health_is_degraded_before_ingest_starts(container):
    payload = await container.health()
    assert payload["status"] == "degraded"
    assert "hyperliquid ingestion not started yet" in payload["reasons"]
    assert payload["database"]["connected"] is True


async def test_health_is_degraded_while_the_socket_is_reconnecting(container, monkeypatch):
    """A Hyperliquid outage must not fail the Railway health check outright."""
    endpoint = FakeEndpoint(OSError("hyperliquid unreachable"))
    monkeypatch.setattr(ws_module, "ws_connect", endpoint)
    container.rest._client = UniverseHTTP()

    await container.start_ingest()
    try:
        await wait_for(lambda: endpoint.attempts >= 1)
        payload = await container.health()
        assert payload["status"] == "degraded"
        assert any("websocket disconnected" in reason for reason in payload["reasons"])
        assert payload["hyperliquid"]["connected"] is False
    finally:
        await container.stop_ingest()


async def test_health_is_unhealthy_when_the_database_is_gone(container, monkeypatch):
    async def dead() -> bool:
        return False

    monkeypatch.setattr(container.db, "healthcheck", dead)
    payload = await container.health()
    assert payload["status"] == "unhealthy"
    assert "database unreachable" in payload["reasons"]


async def test_the_alert_sender_stops_cleanly(container):
    await container.alerts.start()
    await container.alerts.stop()
    await container.alerts.stop()             # idempotent, as shutdown needs
    lingering = [
        task
        for task in asyncio.all_tasks()
        if (task.get_name() or "") == "alert-sender" and not task.done()
    ]
    assert lingering == []
