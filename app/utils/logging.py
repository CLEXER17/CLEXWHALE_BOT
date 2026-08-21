"""Structured logging with secret redaction.

Two output modes:
  * ``LOG_JSON=false`` → human readable single line (local development)
  * ``LOG_JSON=true``  → one JSON object per line (Railway / log shipping)

Never log secrets: the bot token and any database password are scrubbed from
every record by :class:`SecretRedactor` before it reaches a handler.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}

_REDACTED = "***REDACTED***"


class SecretRedactor(logging.Filter):
    """Replace known secret substrings anywhere in a log record."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def register(self, *secrets: str | None) -> None:
        for secret in secrets:
            if secret and len(secret) >= 8 and secret not in self._secrets:
                self._secrets.append(secret)

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        return text

    def _scrub_arg(self, value: Any) -> Any:
        """Redact one ``record.args`` entry **without changing its type**.

        ``record.args`` is consumed as ``msg % args``, so coercing entries to
        ``str`` breaks every numeric placeholder in a third-party log call.
        uvicorn logs ``"Started server process [%d]"``; a stringified pid made
        ``getMessage()`` raise ``TypeError`` inside the formatter, which turned
        each of those lines into a multi-page logging traceback on Railway.

        Strings are scrubbed directly. Anything else is only replaced when it
        actually carries a secret — in which case a redacted string is the
        correct answer regardless of what the placeholder wanted.
        """
        if isinstance(value, str):
            return self._scrub(value)
        try:
            text = str(value)
        except Exception:  # a __str__ that raises must not break logging
            return value
        scrubbed = self._scrub(text)
        return scrubbed if scrubbed != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            if isinstance(record.msg, str):
                record.msg = self._scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: self._scrub_arg(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(self._scrub_arg(a) for a in record.args)
            for key, value in list(record.__dict__.items()):
                if key not in _RESERVED and isinstance(value, str):
                    record.__dict__[key] = self._scrub(value)
        except Exception:  # logging must never raise
            pass
        return True


REDACTOR = SecretRedactor()


def _safe_message(record: logging.LogRecord) -> str:
    """``record.getMessage()`` that cannot raise.

    A single malformed third-party log call — wrong placeholder, wrong arg count
    — otherwise makes the formatter raise for every occurrence, and Python
    answers each one with a full traceback on stderr. One cosmetic mistake then
    buries the real startup log. Degrade to the raw template instead.
    """
    try:
        return record.getMessage()
    except Exception:
        return f"{record.msg!s} [unformattable args: {record.args!r}]"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _safe_message(record),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(name)-28s %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def format(self, record: logging.LogRecord) -> str:
        # Resolve the message safely first, then let the parent do the rest
        # (exception text, stack info). With ``args`` cleared the parent's own
        # ``getMessage()`` call is a no-op and cannot raise on a bad template.
        record.msg = _safe_message(record)
        record.args = ()
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += " | " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Log lines carry coin symbols and emoji. A console using a legacy code page
    # (Windows cp1252) would raise UnicodeEncodeError mid-record, so force UTF-8
    # and never let an unencodable character take down a log call.
    with suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else PlainFormatter())
    handler.addFilter(REDACTOR)
    root.addHandler(handler)

    # Third-party chatter we do not need at INFO.
    for noisy, noisy_level in (
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("websockets.client", logging.WARNING),
        ("telegram.ext.Application", logging.WARNING),
        ("telegram.ext.ExtBot", logging.WARNING),
        ("apscheduler", logging.WARNING),
        ("aiosqlite", logging.WARNING),
        # Alembic's per-plugin setup lines say nothing useful; the migration
        # lines themselves come from alembic.runtime.migration and stay.
        ("alembic.runtime.plugins", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)


def register_secrets(*secrets: str | None) -> None:
    REDACTOR.register(*secrets)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
