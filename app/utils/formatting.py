"""Presentation helpers plus the data-accuracy primitives.

:class:`DataPoint` is the project's answer to development rule "never fabricate
missing information": every optional field carried by an alert is wrapped with
an explicit confidence label, so the renderer can print

    Liquidation: $57,421   (confirmed)
    TP: none detected      (unavailable)
    Margin: ~$482,000      (estimated)

and can never silently pass an estimate off as a trader-set value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class Confidence(str, Enum):
    #: Read directly from a Hyperliquid API response.
    CONFIRMED = "CONFIRMED"
    #: Computed by us from confirmed inputs (documented in ``note``).
    ESTIMATED = "ESTIMATED"
    #: Not exposed by Hyperliquid's public API, or not set by the trader.
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class DataPoint:
    value: Any = None
    confidence: Confidence = Confidence.UNAVAILABLE
    note: str | None = None

    @classmethod
    def confirmed(cls, value: Any, note: str | None = None) -> DataPoint:
        if value is None:
            return cls.unavailable(note)
        return cls(value, Confidence.CONFIRMED, note)

    @classmethod
    def estimated(cls, value: Any, note: str) -> DataPoint:
        if value is None:
            return cls.unavailable(note)
        return cls(value, Confidence.ESTIMATED, note)

    @classmethod
    def unavailable(cls, note: str | None = None) -> DataPoint:
        return cls(None, Confidence.UNAVAILABLE, note)

    @property
    def available(self) -> bool:
        return self.confidence is not Confidence.UNAVAILABLE and self.value is not None

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "confidence": self.confidence.value, "note": self.note}

    @classmethod
    def from_json(cls, raw: Any) -> DataPoint:
        if not isinstance(raw, dict):
            return cls.unavailable()
        try:
            confidence = Confidence(raw.get("confidence", "UNAVAILABLE"))
        except ValueError:
            confidence = Confidence.UNAVAILABLE
        return cls(raw.get("value"), confidence, raw.get("note"))


# ── numbers ────────────────────────────────────────────────────

def to_float(value: Any, default: float | None = None) -> float | None:
    """Hyperliquid returns numbers as strings; parse defensively."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_usd(value: float | None, decimals: int = 2) -> str:
    """Compact money: ``$4.82M``."""
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    v = abs(float(value))
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= size:
            return f"{sign}${v / size:,.{decimals}f}{unit}"
    return f"{sign}${v:,.2f}"


def fmt_usd_full(value: float | None) -> str:
    """Exact money: ``$4,820,000``."""
    if value is None:
        return "N/A"
    return f"{'-' if value < 0 else ''}${abs(float(value)):,.0f}"


def fmt_price(value: float | None) -> str:
    """Price with a sensible number of decimals for the magnitude."""
    if value is None:
        return "N/A"
    v = abs(float(value))
    if v >= 1000:
        decimals = 2
    elif v >= 10:
        decimals = 3
    elif v >= 0.1:
        decimals = 4
    elif v >= 0.001:
        decimals = 6
    else:
        decimals = 8
    text = f"{float(value):,.{decimals}f}"
    if decimals > 2 and "." in text:
        # Drop insignificant zeros but keep cents: $182.510 -> $182.51.
        whole, _, frac = text.partition(".")
        frac = frac.rstrip("0").ljust(2, "0")
        text = f"{whole}.{frac}"
    return f"${text}"


def fmt_size(value: float | None) -> str:
    if value is None:
        return "N/A"
    v = abs(float(value))
    decimals = 2 if v >= 100 else 4 if v >= 1 else 6
    return f"{float(value):,.{decimals}f}".rstrip("0").rstrip(".")


def fmt_pct(value: float | None, decimals: int = 2, signed: bool = True) -> str:
    if value is None:
        return "N/A"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{decimals}f}%"


def fmt_leverage(value: float | None, kind: str | None = None) -> str:
    if value is None:
        return "N/A"
    text = f"{value:g}x"
    if kind:
        text += f" ({kind})"
    return text


def pct_distance(target: float | None, reference: float | None) -> float | None:
    """Signed percentage from ``reference`` to ``target``."""
    if not target or not reference:
        return None
    try:
        return (target - reference) / reference * 100.0
    except ZeroDivisionError:
        return None


# ── identities ─────────────────────────────────────────────────

def wallet_code(address: str | None) -> str:
    """The one canonical way to show a wallet to a human.

    Complete 42-character address, lowercased, monospace so Telegram offers
    tap-to-copy. A truncated address is not actionable — it cannot be pasted
    into a block explorer and two different whales can share a prefix and a
    suffix — so no list view, alert or admin panel abbreviates it.
    """
    if not address:
        return "<code>unknown</code>"
    return f"<code>{escape_html(str(address).strip().lower())}</code>"


def short_wallet(address: str | None, lead: int = 6, tail: int = 4) -> str:
    """Abbreviate an address for places that cannot hold 42 characters.

    Inline-keyboard button labels only. Never for message bodies (use
    :func:`wallet_code`), never for storage, and never as the canonical value:
    the database always keeps the complete address.
    """
    if not address:
        return "unknown"
    if len(address) <= lead + tail + 3:
        return address
    return f"{address[:lead]}...{address[-tail:]}"


def is_hex_address(value: str | None) -> bool:
    if not value or not isinstance(value, str):
        return False
    if not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


# ── time ───────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def from_ms(ms: Any) -> datetime | None:
    value = to_float(ms)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def fmt_time(moment: datetime | None, with_seconds: bool = True) -> str:
    if moment is None:
        return "N/A"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    pattern = "%H:%M:%S UTC" if with_seconds else "%H:%M UTC"
    return moment.astimezone(timezone.utc).strftime(pattern)


def fmt_datetime(moment: datetime | None) -> str:
    if moment is None:
        return "N/A"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_duration(delta: timedelta | float | None) -> str:
    """``3m 12s`` / ``4h 05m`` / ``2d 3h``."""
    if delta is None:
        return "N/A"
    seconds = int(delta.total_seconds() if isinstance(delta, timedelta) else delta)
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def fmt_ago(moment: datetime | None, now: datetime | None = None) -> str:
    if moment is None:
        return "N/A"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"{fmt_duration((now or utc_now()) - moment)} ago"


# ── telegram text ──────────────────────────────────────────────

_MD_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!\\")


def escape_md(text: Any) -> str:
    """Escape for Telegram MarkdownV2."""
    return "".join("\\" + ch if ch in _MD_V2_SPECIAL else ch for ch in str(text))


def escape_html(text: Any) -> str:
    """Escape for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


DIVIDER = "━━━━━━━━━━━━━━━━━━"


def bool_badge(value: bool, on: str = "ON", off: str = "OFF") -> str:
    return f"{'🟢' if value else '🔴'} {on if value else off}"
