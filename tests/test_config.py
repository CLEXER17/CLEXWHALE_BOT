"""Configuration validation and secret hygiene.

Spec §36: "API failure handling" starts with refusing to start on a bad config.
Spec §42/§45: a missing production variable must fail loudly at startup, never
fall back to a fake default. Spec §32: secrets never reach logs or Telegram.
"""

from __future__ import annotations

import logging

import pytest

from app.config import REQUIRED_PRODUCTION_VARS, ConfigError, Settings, validate_runtime
from app.utils.logging import REDACTOR, SecretRedactor
from tests.conftest import FAKE_BOT_TOKEN, MAIN_ADMIN_ID, make_settings


# ── fatal problems ─────────────────────────────────────────────

def test_missing_bot_token_is_fatal():
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(make_settings(bot_token=""))
    message = str(excinfo.value)
    assert "BOT_TOKEN is not set" in message
    assert "refusing to start" in message


def test_malformed_bot_token_is_fatal():
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(make_settings(bot_token="not-a-real-token"))
    assert "does not look like a BotFather token" in str(excinfo.value)


def test_the_token_value_is_never_echoed_in_the_error():
    """§32: an invalid token is still a secret."""
    secret = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA extra"
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(make_settings(bot_token=secret))
    assert secret not in str(excinfo.value)


def test_missing_main_admin_id_is_fatal():
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(make_settings(main_admin_id=0))
    assert "MAIN_ADMIN_ID" in str(excinfo.value)


def test_all_fatal_problems_are_reported_at_once():
    """One deploy attempt should reveal every problem, not just the first."""
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(make_settings(bot_token="", main_admin_id=0, min_whale_value=0))
    message = str(excinfo.value)
    assert "BOT_TOKEN" in message
    assert "MAIN_ADMIN_ID" in message
    assert "MIN_WHALE_VALUE" in message
    assert message.count("  - ") == 3


def test_the_error_names_the_required_variables():
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(make_settings(bot_token=""))
    for name in REQUIRED_PRODUCTION_VARS:
        assert name in str(excinfo.value)


def test_invalid_numbers_are_fatal():
    for override in (
        {"min_whale_value": -1},
        {"alert_cooldown_seconds": 5_000},
        {"port": 0},
        {"port": 70_000},
        {"alert_rate_per_minute": 0},
    ):
        with pytest.raises(ConfigError):
            validate_runtime(make_settings(**override))


# ── production database rules ──────────────────────────────────

def test_production_without_database_url_is_fatal(monkeypatch: pytest.MonkeyPatch):
    """§45: no silent fallback to a local file database in production."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(make_settings(app_env="production"))
    assert "DATABASE_URL is not set" in str(excinfo.value)
    assert "${{Postgres.DATABASE_URL}}" in str(excinfo.value)


def test_production_with_sqlite_is_fatal(monkeypatch: pytest.MonkeyPatch):
    """§45: a container filesystem is wiped on redeploy."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./whalebot.db")
    with pytest.raises(ConfigError) as excinfo:
        validate_runtime(
            make_settings(app_env="production", database_url="sqlite+aiosqlite:///./whalebot.db")
        )
    assert "points at SQLite" in str(excinfo.value)


def test_production_with_postgres_is_accepted(monkeypatch: pytest.MonkeyPatch):
    url = "postgresql+asyncpg://user:pass@host:5432/railway"
    monkeypatch.setenv("DATABASE_URL", url)
    warnings = validate_runtime(make_settings(app_env="production", database_url=url))
    assert isinstance(warnings, list)
    assert not any("SQLite" in w for w in warnings)


def test_local_sqlite_is_only_a_warning():
    warnings = validate_runtime(make_settings(app_env="test"))
    assert any("SQLite" in w for w in warnings)


def test_no_database_credentials_are_hardcoded():
    """§45: the default URL must not embed a host, user, password or port."""
    default = Settings.model_fields["database_url"].default
    assert default.startswith("sqlite")
    for forbidden in ("@", "postgres://", "postgresql://", ":5432", "password"):
        assert forbidden not in default


def test_no_token_default_is_shipped():
    assert Settings.model_fields["bot_token"].default == ""
    assert Settings.model_fields["main_admin_id"].default == 0


# ── warnings (non-fatal) ───────────────────────────────────────

def test_empty_coin_list_warns():
    warnings = validate_runtime(make_settings(default_coins="", monitor_all_coins=False))
    assert any("no coin will be monitored" in w for w in warnings)


def test_all_coins_mode_does_not_warn_about_empty_coins():
    warnings = validate_runtime(make_settings(default_coins="", monitor_all_coins=True))
    assert not any("no coin will be monitored" in w for w in warnings)


def test_zero_focus_wallets_warns_about_lost_tpsl():
    warnings = validate_runtime(make_settings(ws_focus_wallets=0))
    assert any("TP/SL detection are disabled" in w for w in warnings)


def test_a_low_threshold_warns_about_volume():
    warnings = validate_runtime(make_settings(min_whale_value=5_000))
    assert any("high alert volume" in w for w in warnings)


def test_a_sane_config_produces_no_warnings_beyond_sqlite():
    warnings = validate_runtime(make_settings())
    assert [w for w in warnings if "SQLite" not in w] == []


# ── derived values ─────────────────────────────────────────────

def test_environment_classification():
    assert make_settings(app_env="production").is_production is True
    assert make_settings(app_env="Production").is_production is True
    assert make_settings(app_env="test").is_production is False
    assert make_settings(app_env="development").is_production is False


def test_coin_list_is_normalised():
    assert make_settings(default_coins=" btc , eth,, sol ").default_coin_list == [
        "BTC",
        "ETH",
        "SOL",
    ]


def test_rest_weight_budget_applies_the_safety_factor():
    settings = make_settings(rest_weight_per_minute=1200, rest_weight_safety=0.5)
    assert settings.rest_weight_budget == 600.0


def test_settings_ignore_unknown_environment_variables(monkeypatch: pytest.MonkeyPatch):
    """Railway injects many variables; unknown ones must not crash startup."""
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "abc123")
    settings = Settings(_env_file=None, bot_token=FAKE_BOT_TOKEN, main_admin_id=MAIN_ADMIN_ID)
    assert settings.main_admin_id == MAIN_ADMIN_ID


# ── secret redaction ───────────────────────────────────────────

def test_redactor_scrubs_a_registered_secret_from_a_message():
    redactor = SecretRedactor()
    redactor.register(FAKE_BOT_TOKEN)
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, f"calling https://api/{FAKE_BOT_TOKEN}/getMe", None, None
    )
    redactor.filter(record)
    assert FAKE_BOT_TOKEN not in str(record.msg)
    assert "***REDACTED***" in str(record.msg)


def test_redactor_scrubs_extra_fields():
    redactor = SecretRedactor()
    redactor.register(FAKE_BOT_TOKEN)
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "boom", None, None)
    record.url = f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/sendMessage"
    redactor.filter(record)
    assert FAKE_BOT_TOKEN not in record.url


def test_redactor_ignores_short_strings():
    """Registering "1" must not turn every log line into redaction confetti."""
    redactor = SecretRedactor()
    redactor.register("1", "abc")
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "user 1 abc", None, None)
    redactor.filter(record)
    assert str(record.msg) == "user 1 abc"


def test_redactor_never_raises():
    redactor = SecretRedactor()
    redactor.register(FAKE_BOT_TOKEN)

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    record = logging.LogRecord("test", logging.INFO, __file__, 1, Hostile(), None, None)
    assert redactor.filter(record) is True


def test_the_global_redactor_is_installed():
    assert isinstance(REDACTOR, SecretRedactor)
