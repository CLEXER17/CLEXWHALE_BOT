"""The service container.

One object wires everything together so ``app.main`` and the Telegram handlers
share exactly the same instances: one database pool, one settings snapshot, one
admin role cache, one alert queue, one Hyperliquid engine.

Handlers reach it through ``context.bot_data["app"]`` rather than importing
globals, which is what makes them testable with a stub container.

Startup order matters and is fixed here (spec §48): environment validation
happens before this object exists, then PostgreSQL, then migrations, then the
role/settings caches are restored from the database, then Telegram, then
Hyperliquid. Nothing critical is held only in RAM — every value the container
caches was read from the database and is written back on change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import Settings, get_settings
from app.database.base import Database
from app.hyperliquid.rest import HyperliquidREST
from app.services.admin_service import AdminService
from app.services.alert_service import AlertService
from app.services.settings_service import SettingsService
from app.utils.formatting import utc_now
from app.utils.logging import get_logger
from app.utils.ratelimit import WeightedRateLimiter
from app.whale.engine import WhaleEngine

log = get_logger(__name__)


class AppContainer:
    """Owns every long-lived service and their start/stop ordering."""

    def __init__(self, env: Settings | None = None, database: Database | None = None) -> None:
        self.env = env or get_settings()
        self.db = database or Database(
            self.env.database_url,
            pool_size=self.env.db_pool_size,
            max_overflow=self.env.db_max_overflow,
        )

        self.settings = SettingsService(self.db, self.env)
        self.admins = AdminService(self.db, self.env)
        self.alerts = AlertService(self.env, self.db, self.settings, self.admins)

        self.limiter = WeightedRateLimiter(self.env.rest_weight_budget)
        self.rest = HyperliquidREST(self.env.hyperliquid_api_url, self.limiter)
        self.engine = WhaleEngine(
            self.env,
            self.db,
            self.settings,
            self.rest,
            alert_callback=self.alerts.enqueue,
        )

        self.started_at: datetime | None = None
        self.ingest_started = False
        #: Non-fatal configuration warnings surfaced by /health and /status.
        self.warnings: list[str] = []

    # ── state restored from the database ──────────────────────
    async def restore(self) -> None:
        """Reload persisted settings, coins, wallets and admin roles.

        Called once after migrations and again is harmless. This is what makes a
        Railway redeploy transparent: the monitoring switch, threshold, coin
        selection, public mode, co-admins and tracked wallets all come back
        exactly as the administrator left them.
        """
        await self.admins.load()
        config = await self.settings.load()
        log.info(
            "Runtime state restored",
            extra={
                "monitoring": config.monitoring_enabled,
                "public_mode": config.public_mode,
                "coins": config.coin_label,
                "threshold": config.min_whale_value,
                "co_admins": self.admins.co_admin_count,
                "tracked_wallets": len(config.tracked_wallets),
            },
        )

    # ── ingestion ─────────────────────────────────────────────
    async def start_ingest(self) -> None:
        """Bring up Hyperliquid. Failure here degrades the bot, never kills it."""
        if self.ingest_started:
            return
        await self.rest.start()
        await self.engine.start()
        self.ingest_started = True

    async def stop_ingest(self) -> None:
        if not self.ingest_started:
            return
        await self.engine.stop()
        await self.rest.aclose()
        self.ingest_started = False

    # ── health ────────────────────────────────────────────────
    @property
    def uptime_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (utc_now() - self.started_at).total_seconds()

    async def health(self) -> dict[str, Any]:
        """Payload for ``GET /health``.

        * ``unhealthy`` — the database is unreachable, so nothing works.
        * ``degraded`` — the bot answers but Hyperliquid is disconnected, so no
          new whale data is arriving. Reconnection keeps running in the
          background; this is not a reason to fail the deploy.
        * ``healthy`` — database up, Hyperliquid connected.
        """
        db_ok = await self.db.healthcheck()
        hl_connected = self.engine.connected if self.ingest_started else False

        if not db_ok:
            status = "unhealthy"
        elif not self.ingest_started or not hl_connected:
            status = "degraded"
        else:
            status = "healthy"

        reasons: list[str] = []
        if not db_ok:
            reasons.append("database unreachable")
        if self.ingest_started and not hl_connected:
            reasons.append("hyperliquid websocket disconnected (reconnecting)")
        if not self.ingest_started:
            reasons.append("hyperliquid ingestion not started yet")

        return {
            "status": status,
            "reasons": reasons,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "database": {"connected": db_ok, **self.db.stats()},
            "hyperliquid": {
                "connected": hl_connected,
                "rest_healthy": self.rest.healthy,
            },
            "monitoring_enabled": self.settings.config.monitoring_enabled,
            "public_mode": self.settings.config.public_mode,
            "alerts": self.alerts.stats(),
            "warnings": self.warnings,
        }

    def stats(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(self.uptime_seconds, 1),
            "engine": self.engine.stats(),
            "alerts": self.alerts.stats(),
            "admins": self.admins.stats(),
            "database": self.db.stats(),
        }
