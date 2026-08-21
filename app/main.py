"""Process entry point.

One Railway service runs everything: the Telegram bot, the Hyperliquid
ingestion pipeline and a small HTTP server for ``/health``. There is no separate
worker, no cron job and no script an operator has to start by hand — ``python -m
app.main`` is the whole production command.

Startup order (spec §48) is deliberate and enforced here:

1. Validate the environment. A missing credential aborts the boot; nothing
   falls back to a placeholder.
2. Connect to PostgreSQL and wait for it — Railway's database may still be
   booting when the app container is already up.
3. Run migrations (Alembic ``upgrade head``).
4. Restore persisted state: admins, co-admins, settings, coins, tracked
   wallets. Nothing critical lives only in RAM.
5. Start the HTTP server, so ``/health`` answers as early as possible.
6. Start Telegram.
7. Start Hyperliquid ingestion last: it is the only component whose failure is
   survivable, and its absence is reported as ``degraded`` rather than fatal.

Shutdown runs in reverse on SIGTERM/SIGINT, which is what Railway sends on
redeploy.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import suppress
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from app.bot.application import build_application, post_init
from app.config import ConfigError, Settings, get_settings, validate_runtime
from app.container import AppContainer
from app.database.base import set_database
from app.utils.formatting import utc_now
from app.utils.logging import get_logger, register_secrets, setup_logging

log = get_logger(__name__)

#: How long we wait for Railway's Postgres before giving up on the boot.
DB_WAIT_ATTEMPTS = 12
DB_WAIT_DELAY = 2.0

#: HTTP status codes returned by ``/health`` for each state. ``degraded`` stays
#: 200: the bot is answering, and a platform health check must not restart it
#: just because the Hyperliquid socket is mid-reconnect.
_HEALTH_STATUS = {"healthy": 200, "degraded": 200, "unhealthy": 503}


# ── HTTP surface ───────────────────────────────────────────────
def build_http_app(container: AppContainer) -> FastAPI:
    """Minimal HTTP app: health, readiness, statistics.

    No secrets are ever included in a response — only connection booleans,
    counters and configuration values an operator may safely see.
    """
    http = FastAPI(
        title="Hyperliquid Whale Monitor",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @http.get("/health")
    async def health() -> JSONResponse:
        payload = await container.health()
        return JSONResponse(payload, status_code=_HEALTH_STATUS.get(payload["status"], 503))

    @http.get("/ready")
    async def ready() -> JSONResponse:
        """Liveness for the platform: is the process up and the database usable?"""
        ok = await container.db.healthcheck()
        return JSONResponse({"ready": ok}, status_code=200 if ok else 503)

    @http.get("/stats")
    async def stats() -> dict[str, Any]:
        return container.stats()

    @http.get("/")
    async def root() -> PlainTextResponse:
        config = container.settings.config
        state = "monitoring" if config.monitoring_enabled else "idle"
        return PlainTextResponse(f"Hyperliquid Whale Monitor — {state}\n")

    return http


# ── migrations ─────────────────────────────────────────────────
def _run_migrations_sync(database_url: str) -> None:
    """``alembic upgrade head`` in-process.

    Run in a worker thread: Alembic's async env driver calls ``asyncio.run``,
    which cannot be nested inside the running loop.
    """
    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "app", "database", "migrations"))
    # env.py reads DATABASE_URL itself; setting it here keeps offline mode honest
    # and escapes '%' so ConfigParser interpolation cannot mangle a password.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    # Our handlers (with the secret-redaction filter) are already installed;
    # env.py must not replace them with alembic.ini's logging section.
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")


async def run_migrations(container: AppContainer) -> None:
    """Bring the schema up to date, or fail the boot with a clear reason."""
    if not container.env.run_migrations:
        log.warning("RUN_MIGRATIONS is false — skipping schema upgrade")
        return
    log.info("Applying database migrations")
    await asyncio.to_thread(_run_migrations_sync, container.env.database_url)
    log.info("Database schema is up to date")


# ── lifecycle ──────────────────────────────────────────────────
class Runtime:
    """Owns the three long-lived components and their ordering."""

    def __init__(self, env: Settings) -> None:
        self.env = env
        self.container = AppContainer(env)
        set_database(self.container.db)
        self.telegram = build_application(self.container)
        self.http = build_http_app(self.container)
        self.server: uvicorn.Server | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        container = self.container

        # 2. database
        await container.db.connect()
        if not await container.db.wait_until_ready(DB_WAIT_ATTEMPTS, DB_WAIT_DELAY):
            raise ConfigError(
                "Could not reach the database at DATABASE_URL after "
                f"{DB_WAIT_ATTEMPTS} attempts. Last error: {container.db.last_error}"
            )
        log.info("Database connected", extra=container.db.stats())

        # 3. migrations
        await run_migrations(container)

        # 4. persisted state
        await container.restore()

        # 5. HTTP first, so the platform's health check has something to talk to
        await self._start_http()

        # 6. Telegram
        await self.telegram.initialize()
        # PTB runs ``post_init`` only from ``run_polling``/``run_webhook``; this
        # process owns the lifecycle, so it has to run it. Identity is available
        # now because ``initialize()`` has already called ``getMe``.
        await post_init(self.telegram)
        await container.alerts.start()
        await self.telegram.start()
        if self.telegram.updater is not None:
            await self.telegram.updater.start_polling(
                drop_pending_updates=True,
                # Nothing here reacts to channel posts or edits.
                allowed_updates=["message", "callback_query"],
            )
        log.info("Telegram polling started")

        container.started_at = utc_now()

        # 7. Hyperliquid last. A failure here degrades the service; the bot still
        #    answers and the engine keeps retrying in the background.
        try:
            await container.start_ingest()
        except Exception:
            log.exception("Hyperliquid ingestion failed to start; running degraded")

        config = container.settings.config
        log.info(
            "Startup complete",
            extra={
                "monitoring": config.monitoring_enabled,
                "public_mode": config.public_mode,
                "threshold": config.min_whale_value,
                "coins": config.coin_label,
                "port": self.env.port,
            },
        )

    async def _start_http(self) -> None:
        config = uvicorn.Config(
            self.http,
            host=self.env.http_host,
            port=self.env.port,
            log_level=self.env.log_level.lower(),
            # Our own handlers are already installed; uvicorn must not replace
            # them or the redaction filter would be dropped.
            log_config=None,
            access_log=False,
            # Signals are handled by Runtime, not by the server.
            lifespan="off",
        )
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = False
        self._server_task = asyncio.create_task(self.server.serve(), name="http-server")
        # Give it a moment to bind so a port clash is reported at startup.
        for _ in range(50):
            if self.server.started:
                break
            if self._server_task.done():
                await self._server_task  # re-raise the bind error
            await asyncio.sleep(0.1)
        log.info("HTTP server listening", extra={"port": self.env.port, "path": "/health"})

    async def stop(self) -> None:
        """Reverse of :meth:`start`; every step is best-effort."""
        if self._stopping:
            return
        self._stopping = True
        log.info("Shutting down")

        with suppress(Exception):
            await self.container.stop_ingest()

        if self.telegram.updater is not None and self.telegram.updater.running:
            with suppress(Exception):
                await self.telegram.updater.stop()
        with suppress(Exception):
            await self.container.alerts.stop()
        if self.telegram.running:
            with suppress(Exception):
                await self.telegram.stop()
        with suppress(Exception):
            await self.telegram.shutdown()

        if self.server is not None:
            self.server.should_exit = True
        if self._server_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._server_task, timeout=10.0)

        with suppress(Exception):
            await self.container.db.disconnect()
        log.info("Shutdown complete")


async def run() -> int:
    env = get_settings()
    setup_logging(env.log_level, env.log_json)
    # Register before anything else can log: the token and the database
    # credentials are scrubbed out of every record from here on.
    register_secrets(env.bot_token, _password_of(env.database_url), env.database_url)

    # 1. environment validation — before a single connection is opened.
    try:
        warnings = validate_runtime(env)
    except ConfigError as exc:
        log.error("Configuration error", extra={"detail": str(exc)})
        # Also to stderr: on a fresh Railway deploy this is the only thing the
        # operator will look at.
        print(str(exc), file=sys.stderr, flush=True)
        return 78  # EX_CONFIG

    for warning in warnings:
        log.warning("Configuration warning", extra={"detail": warning})

    runtime = Runtime(env)
    runtime.container.warnings = list(warnings)

    stop_signal = asyncio.Event()
    _install_signal_handlers(stop_signal)

    try:
        await runtime.start()
    except Exception:
        log.exception("Startup failed")
        await runtime.stop()
        return 1

    await stop_signal.wait()
    await runtime.stop()
    return 0


def _install_signal_handlers(stop_signal: asyncio.Event) -> None:
    """SIGTERM is what Railway sends on redeploy; SIGINT is Ctrl-C locally."""
    loop = asyncio.get_running_loop()

    def _request_stop(signum: int) -> None:
        log.info("Signal received", extra={"signal": signal.Signals(signum).name})
        stop_signal.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
        except NotImplementedError:
            # Windows: only the default synchronous handler is available.
            signal.signal(sig, lambda signum, _frame: _request_stop(signum))


def _password_of(database_url: str) -> str | None:
    """Extract the password from a URL so the redactor can scrub it."""
    from urllib.parse import urlsplit

    with suppress(Exception):
        return urlsplit(database_url).password
    return None


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:  # pragma: no cover - local Ctrl-C
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
