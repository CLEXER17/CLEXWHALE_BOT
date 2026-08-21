"""Process configuration.

Everything here comes from the environment. Values that an administrator can
change at runtime (threshold, coins, public mode, ...) are seeded from these
env vars on first boot and afterwards live in the database — see
``app.services.settings_service``.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Documented Hyperliquid limit: at most 10 unique addresses across all
# user-specific websocket subscriptions per IP.
WS_UNIQUE_USER_HARD_CAP = 10

#: Env vars that must be supplied by the deployment. There are no defaults for
#: these on purpose: a missing credential must fail the boot, not fall back to
#: something fake.
REQUIRED_PRODUCTION_VARS = ("BOT_TOKEN", "MAIN_ADMIN_ID", "DATABASE_URL")

#: ``<bot id>:<secret>`` as issued by BotFather. Shape only — never logged.
_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

_NON_PRODUCTION_ENVS = {"dev", "develop", "development", "local", "test", "testing", "ci"}


class ConfigError(RuntimeError):
    """A fatal configuration problem. Raised during startup, never swallowed."""



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Telegram ───────────────────────────────────────────────
    bot_token: str = ""
    main_admin_id: int = 0

    # ── Database ───────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./whalebot.db"
    run_migrations: bool = True
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # ── Access control ─────────────────────────────────────────
    public_mode: bool = False

    # ── Thresholds (USD) ───────────────────────────────────────
    min_whale_value: float = 2_000_000.0
    min_trade_value: float | None = None
    min_position_value: float | None = None
    min_order_value: float | None = None
    min_position_delta_value: float | None = None

    #: Minimum ``marginUsed`` (collateral at risk) a position must carry before
    #: it alerts. ``0`` disables the gate. Seeds the database on first boot only.
    min_margin_value: float = 0.0

    # ── Alerting ───────────────────────────────────────────────
    alert_cooldown_seconds: int = 30
    alert_rate_per_minute: int = 20

    # ── Coins ──────────────────────────────────────────────────
    default_coins: str = "BTC,ETH,SOL"
    monitor_all_coins: bool = False
    max_monitored_coins: int = 40

    # ── Detector toggles ───────────────────────────────────────
    enable_trade_detector: bool = True
    enable_position_detector: bool = True
    enable_order_detector: bool = True
    enable_order_cancel_alerts: bool = True
    enable_wallet_tracking: bool = True
    enable_book_scanner: bool = False

    # ── Hyperliquid ────────────────────────────────────────────
    hyperliquid_api_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    rest_weight_per_minute: int = 1200
    rest_weight_safety: float = 0.5
    ws_focus_wallets: int = 8
    position_poll_interval: int = 20
    order_poll_interval: int = 45
    book_poll_interval: int = 30
    wallet_cache_size: int = 400
    wallet_idle_ttl: int = 3600

    # ── Runtime ────────────────────────────────────────────────
    app_env: str = "production"
    log_level: str = "INFO"
    log_json: bool = False
    port: int = 8080
    http_host: str = "0.0.0.0"

    # Set by the test-suite / CLI to skip network side effects.
    offline: bool = Field(default=False, exclude=True)

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_database_url(cls, value: object) -> str:
        """Railway hands out ``postgres://`` URLs; SQLAlchemy needs a driver."""
        if not value:
            return "sqlite+aiosqlite:///./whalebot.db"
        url = str(value).strip()
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://") :]
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        elif url.startswith("sqlite://") and "+aiosqlite" not in url:
            url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        # asyncpg does not understand libpq query args; strip the common ones.
        if url.startswith("postgresql+asyncpg://") and "?" in url:
            base, _, query = url.partition("?")
            kept = [
                part
                for part in query.split("&")
                if part and not part.startswith(("sslmode=", "channel_binding="))
            ]
            url = base + ("?" + "&".join(kept) if kept else "")
        return url

    @field_validator("ws_focus_wallets")
    @classmethod
    def _cap_focus_wallets(cls, value: int) -> int:
        return max(0, min(value, WS_UNIQUE_USER_HARD_CAP))

    @field_validator("rest_weight_safety")
    @classmethod
    def _clamp_safety(cls, value: float) -> float:
        return min(max(value, 0.05), 1.0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    # ── Derived helpers ────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() not in _NON_PRODUCTION_ENVS

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def default_coin_list(self) -> list[str]:
        return [c.strip().upper() for c in self.default_coins.split(",") if c.strip()]

    @property
    def rest_weight_budget(self) -> float:
        """Weight units per minute we allow ourselves to spend."""
        return self.rest_weight_per_minute * self.rest_weight_safety

    def threshold_for(self, kind: str) -> float:
        """Env-level default threshold for a detector class."""
        override = {
            "trade": self.min_trade_value,
            "position": self.min_position_value,
            "position_delta": self.min_position_delta_value,
            "order": self.min_order_value,
        }.get(kind)
        return float(override) if override else float(self.min_whale_value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache — used by tests that patch the environment."""
    get_settings.cache_clear()
    return get_settings()


def validate_runtime(settings: Settings | None = None) -> list[str]:
    """Fail the boot on missing or unusable production configuration.

    Called first in :func:`app.main.startup`, before anything connects. Raises
    :class:`ConfigError` with every problem listed at once, so a misconfigured
    deploy tells the operator what to fix in one go rather than one restart at a
    time. Returns non-fatal warnings for the log.

    Nothing here ever falls back to a placeholder credential or a local
    database: a production deploy with no ``DATABASE_URL`` is an error, not a
    reason to quietly start writing to a container-local SQLite file that
    disappears on the next redeploy.
    """
    settings = settings or get_settings()
    fatal: list[str] = []
    warnings: list[str] = []
    production = settings.is_production

    if not settings.bot_token.strip():
        fatal.append(
            "BOT_TOKEN is not set. Create a bot with @BotFather and set BOT_TOKEN "
            "in the deployment environment (never commit it)."
        )
    elif not _BOT_TOKEN_RE.match(settings.bot_token.strip()):
        # Shape check only. The value itself is never logged or echoed.
        fatal.append(
            "BOT_TOKEN does not look like a BotFather token "
            "(expected '<digits>:<secret>'). Check for stray quotes or whitespace."
        )

    if settings.main_admin_id <= 0:
        fatal.append(
            "MAIN_ADMIN_ID is not set to a valid Telegram user ID. "
            "Send /start to @userinfobot to find yours."
        )

    raw_database_url = (os.getenv("DATABASE_URL") or "").strip()
    if production:
        if not raw_database_url:
            fatal.append(
                "DATABASE_URL is not set. Add a PostgreSQL database to the Railway "
                "project and reference it as ${{Postgres.DATABASE_URL}}."
            )
        elif settings.is_sqlite:
            fatal.append(
                "DATABASE_URL points at SQLite. Production state must live in "
                "PostgreSQL; container filesystems are wiped on every redeploy."
            )
    elif settings.is_sqlite:
        warnings.append("Using SQLite — fine for local runs, not for production.")

    if settings.min_whale_value <= 0:
        fatal.append("MIN_WHALE_VALUE must be greater than 0.")
    if not 0 <= settings.alert_cooldown_seconds <= 3600:
        fatal.append("ALERT_COOLDOWN_SECONDS must be between 0 and 3600.")
    if not 1 <= settings.port <= 65535:
        fatal.append(f"PORT must be between 1 and 65535 (got {settings.port}).")
    if settings.alert_rate_per_minute < 1:
        fatal.append("ALERT_RATE_PER_MINUTE must be at least 1.")

    if not settings.default_coin_list and not settings.monitor_all_coins:
        warnings.append(
            "DEFAULT_COINS is empty and MONITOR_ALL_COINS is off; "
            "no coin will be monitored until an admin runs /setcoins."
        )
    if settings.ws_focus_wallets == 0:
        warnings.append(
            "WS_FOCUS_WALLETS is 0: live order updates and TP/SL detection are disabled."
        )
    if settings.min_whale_value < 100_000:
        warnings.append(
            f"MIN_WHALE_VALUE is {settings.min_whale_value:,.0f}; expect a high alert volume."
        )

    if fatal:
        detail = "\n".join(f"  - {problem}" for problem in fatal)
        raise ConfigError(
            "Invalid configuration; refusing to start:\n"
            f"{detail}\n"
            f"Required environment variables: {', '.join(REQUIRED_PRODUCTION_VARS)}"
        )
    return warnings
