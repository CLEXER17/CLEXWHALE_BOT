"""Hyperliquid REST (info endpoint) client.

All info requests are ``POST {api}/info`` with a JSON body whose ``type``
selects the query. Every call is metered through a shared
:class:`~app.utils.ratelimit.WeightedRateLimiter` using the documented weights
(``clearinghouseState``/``l2Book``/``allMids`` = 2, most others = 20) against a
1200/minute per-IP budget.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.hyperliquid import parser
from app.hyperliquid.constants import INFO_PATH, info_weight
from app.hyperliquid.models import (
    AccountState,
    AssetContext,
    AssetMeta,
    Fill,
    L2Book,
    OpenOrder,
)
from app.utils.backoff import ExponentialBackoff
from app.utils.formatting import utc_now
from app.utils.logging import get_logger
from app.utils.ratelimit import WeightedRateLimiter

log = get_logger(__name__)


class HyperliquidRestError(RuntimeError):
    pass


class HyperliquidREST:
    def __init__(
        self,
        api_url: str,
        limiter: WeightedRateLimiter,
        timeout: float = 15.0,
        max_retries: int = 3,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.limiter = limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        # health / observability
        self.requests = 0
        self.errors = 0
        self.throttled = 0
        self.skipped_no_budget = 0
        self.last_error: str | None = None
        self.last_success_at = None
        self.connected = False

    # ── lifecycle ─────────────────────────────────────────────
    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                timeout=httpx.Timeout(self.timeout),
                headers={"Content-Type": "application/json"},
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.connected = False

    async def __aenter__(self) -> HyperliquidREST:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ── core ──────────────────────────────────────────────────
    async def post_info(
        self,
        request_type: str,
        payload: dict[str, Any] | None = None,
        *,
        wait_for_budget: bool = True,
        budget_timeout: float = 30.0,
    ) -> Any | None:
        """POST one info request. Returns ``None`` on failure (never raises).

        With ``wait_for_budget=False`` the call is dropped instead of queued
        when the weight budget is exhausted — right for opportunistic polling
        where a stale snapshot is better than a backlog.
        """
        if self._client is None:
            await self.start()
        assert self._client is not None

        weight = info_weight(request_type)
        if wait_for_budget:
            if not await self.limiter.acquire(weight, timeout=budget_timeout):
                self.skipped_no_budget += 1
                log.warning("REST budget timeout, dropping request", extra={"request": request_type})
                return None
        elif not self.limiter.try_acquire(weight):
            self.skipped_no_budget += 1
            return None

        body = {"type": request_type, **(payload or {})}
        backoff = ExponentialBackoff(base=0.5, maximum=8.0)

        for attempt in range(1, self.max_retries + 1):
            try:
                self.requests += 1
                response = await self._client.post(INFO_PATH, json=body)
                if response.status_code == 429:
                    self.throttled += 1
                    retry_after = float(response.headers.get("Retry-After", 0) or 0)
                    delay = retry_after if retry_after > 0 else backoff.next_delay()
                    log.warning(
                        "Hyperliquid rate limited",
                        extra={"request": request_type, "retry_in": round(delay, 2)},
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 500:
                    raise HyperliquidRestError(f"HTTP {response.status_code}")
                if response.status_code >= 400:
                    # 4xx other than 429 will not fix itself on retry.
                    self.errors += 1
                    self.last_error = f"{request_type}: HTTP {response.status_code}"
                    log.error(
                        "Hyperliquid rejected request",
                        extra={"request": request_type, "status": response.status_code},
                    )
                    return None
                data = response.json()
                self.connected = True
                self.last_success_at = utc_now()
                return data
            except (httpx.HTTPError, HyperliquidRestError, ValueError) as exc:
                self.errors += 1
                self.last_error = f"{request_type}: {type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    self.connected = False
                    log.warning(
                        "Hyperliquid request failed",
                        extra={"request": request_type, "attempts": attempt, "error": str(exc)},
                    )
                    return None
                await backoff.sleep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self.errors += 1
                self.last_error = f"{request_type}: {type(exc).__name__}: {exc}"
                log.exception("Unexpected error in Hyperliquid request", extra={"request": request_type})
                return None
        return None

    # ── typed endpoints ───────────────────────────────────────
    async def meta(self) -> list[AssetMeta]:
        return parser.parse_meta(await self.post_info("meta") or {})

    async def meta_and_asset_ctxs(self) -> tuple[list[AssetMeta], dict[str, AssetContext]]:
        return parser.parse_meta_and_asset_ctxs(await self.post_info("metaAndAssetCtxs"))

    async def all_mids(self, *, wait_for_budget: bool = True) -> dict[str, float]:
        raw = await self.post_info("allMids", wait_for_budget=wait_for_budget)
        return parser.parse_all_mids(raw)

    async def clearinghouse_state(
        self, user: str, *, wait_for_budget: bool = True
    ) -> AccountState | None:
        raw = await self.post_info(
            "clearinghouseState", {"user": user}, wait_for_budget=wait_for_budget
        )
        if raw is None:
            return None
        return parser.parse_clearinghouse_state(user, raw)

    async def frontend_open_orders(
        self, user: str, *, wait_for_budget: bool = True
    ) -> list[OpenOrder] | None:
        raw = await self.post_info(
            "frontendOpenOrders", {"user": user}, wait_for_budget=wait_for_budget
        )
        if raw is None:
            return None
        return parser.parse_open_orders(raw)

    async def open_orders(self, user: str) -> list[OpenOrder] | None:
        raw = await self.post_info("openOrders", {"user": user})
        if raw is None:
            return None
        return parser.parse_open_orders(raw)

    async def l2_book(
        self, coin: str, n_sig_figs: int | None = None, *, wait_for_budget: bool = True
    ) -> L2Book | None:
        payload: dict[str, Any] = {"coin": coin}
        if n_sig_figs is not None:
            payload["nSigFigs"] = n_sig_figs
        raw = await self.post_info("l2Book", payload, wait_for_budget=wait_for_budget)
        if raw is None:
            return None
        return parser.parse_l2_book(raw)

    async def user_fills(self, user: str, aggregate_by_time: bool = False) -> list[Fill] | None:
        raw = await self.post_info(
            "userFills", {"user": user, "aggregateByTime": aggregate_by_time}
        )
        if raw is None:
            return None
        return parser.parse_fills(raw)

    async def candle_snapshot(
        self, coin: str, interval: str, start_ms: int, end_ms: int | None = None
    ) -> list[dict[str, Any]]:
        req: dict[str, Any] = {"coin": coin, "interval": interval, "startTime": start_ms}
        if end_ms is not None:
            req["endTime"] = end_ms
        raw = await self.post_info("candleSnapshot", {"req": req})
        return raw if isinstance(raw, list) else []

    async def order_status(self, user: str, oid: int | str) -> dict[str, Any] | None:
        raw = await self.post_info("orderStatus", {"user": user, "oid": oid})
        return raw if isinstance(raw, dict) else None

    # ── health ────────────────────────────────────────────────
    @property
    def healthy(self) -> bool:
        return self.connected

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "throttled": self.throttled,
            "skipped_no_budget": self.skipped_no_budget,
            "weight_spent_last_minute": round(self.limiter.spent, 1),
            "weight_budget": round(self.limiter.budget, 1),
            "last_error": self.last_error,
        }
