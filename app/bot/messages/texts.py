"""User-facing message bodies.

Pure functions: state in, HTML string out. Keeping them free of I/O means the
exact wording the spec pins down (§8, §9, §11, §12, §15, §16, §27, §32) can be
asserted in tests without a database or a Telegram connection.

Two rules run through everything here:

* Numbers are rendered from real data or shown as ``N/A``. Nothing is invented
  to make a panel look complete.
* Observation windows are described as what they are — how long *this monitor*
  has watched something — never as candle timeframes.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any, Mapping, Sequence

from app.services.settings_service import RuntimeConfig
from app.utils.formatting import (
    DIVIDER,
    escape_html,
    fmt_ago,
    fmt_duration,
    fmt_price,
    fmt_time,
    fmt_usd,
    fmt_usd_full,
    wallet_code,
)

# ── access control ─────────────────────────────────────────────
PRIVATE_NOTICE = (
    "🔒 This bot is currently private.\n"
    "Whale monitoring is available only to authorized administrators."
)

NOT_AUTHORIZED = "🚫 You are not authorized to use this control."

CO_ADMIN_REFUSED = (
    "🚫 Only the Main Admin can manage administrators.\n"
    "Co-Admins cannot add or remove other admins."
)

# ── prompts (spec §15) ─────────────────────────────────────────
PROMPT_ADD_ADMIN = "Send the Telegram User ID to add as Co-Admin."
PROMPT_REMOVE_ADMIN = "Send the Telegram User ID of the Co-Admin to remove."
PROMPT_THRESHOLD = (
    "Send the minimum whale value in USD.\n"
    "Example: <code>2000000</code> for $2,000,000."
)
PROMPT_MARGIN = (
    "Send the minimum <b>margin</b> in USD — the collateral a position must\n"
    "have at risk before it alerts.\n"
    "Example: <code>2000000</code> for $2,000,000. Send <code>0</code> to turn the gate off."
)
PROMPT_COINS = (
    "Send the coins to <b>add</b>, separated by spaces.\n"
    "Example: <code>BTC ETH SOL</code>\n"
    "\n"
    "These are added to what is already monitored — nothing is removed.\n"
    "To remove one, tap it in the coin list. To replace the whole list,\n"
    "use <code>/setcoins BTC ETH</code>."
)
PROMPT_COOLDOWN = "Send the alert cooldown in seconds (0–3600)."
PROMPT_WALLET = "Send the wallet address to watch (0x…)."
CANCELLED = "✖️ Cancelled."


# ── alert subscription (/stop, /start) ─────────────────────────
def alerts_stopped() -> str:
    return "\n".join(
        [
            "🔇 <b>Alerts stopped.</b>",
            "",
            "You will not receive any further whale signals in this chat.",
            "Send /start when you want them back — the setting is stored, so it",
            "survives restarts and redeployments.",
        ]
    )


def alerts_resumed() -> str:
    return "🔔 <b>Alerts resumed.</b> You will receive whale signals again."


def start_admin(role_label: str, config: RuntimeConfig) -> str:
    return "\n".join(
        [
            "🐋 <b>HYPERLIQUID WHALE MONITOR</b>",
            DIVIDER,
            f"Signed in as: <b>{escape_html(role_label)}</b>",
            f"Monitoring: {'🟢 ON' if config.monitoring_enabled else '🔴 OFF'}",
            f"Threshold: <b>{fmt_usd_full(config.min_whale_value)}</b>",
            f"Coins: <b>{escape_html(config.coin_label)}</b>",
            f"Access: {'🌐 PUBLIC' if config.public_mode else '🔒 PRIVATE'}",
            DIVIDER,
            "Use the control panel below, or /help for the full command list.",
        ]
    )


def start_public() -> str:
    """Spec §12 — the public entry screen."""
    return "\n".join(["🐋 <b>HYPERLIQUID WHALE MONITOR</b>", "", "Choose an option:"])


def about(config: RuntimeConfig) -> str:
    """Honest capability statement, including what Hyperliquid cannot tell us."""
    return "\n".join(
        [
            "ℹ️ <b>ABOUT</b>",
            DIVIDER,
            "Live whale monitoring for Hyperliquid perpetual futures, built on the",
            "public Hyperliquid API — the <code>trades</code> websocket feed for",
            "wallet-attributed fills, and <code>/info</code> for positions and",
            "resting orders.",
            "",
            f"Minimum whale value: <b>{fmt_usd_full(config.min_whale_value)}</b>",
            f"Coins: <b>{escape_html(config.coin_label)}</b>",
            "",
            "<b>What is reported</b>",
            "• Large market trades, with the wallet that filled them",
            "• Positions opened, increased, reduced, closed or flipped",
            "• Large resting limit orders: placed, modified, filled, cancelled",
            "• Entry price, size, notional, leverage, margin and liquidation price",
            "  exactly as Hyperliquid reports them",
            "",
            "<b>Known limits</b>",
            "• Take-profit and stop-loss are only visible when the trader leaves a",
            "  resting trigger order. Otherwise the alert says <b>N/A</b> — nothing",
            "  is estimated in their place.",
            "• Hyperliquid caps per-wallet subscriptions, so live order streams",
            "  cover only the most active wallets at any moment.",
            "• Order-book depth is aggregated by the exchange and carries no",
            "  wallet address.",
            "• Durations are how long this monitor has observed something, not",
            "  chart timeframes.",
            "",
            "No wallet is ever linked to a real-world person or company.",
        ]
    )


def help_text(*, admin: bool, main_admin: bool) -> str:
    lines = [
        "📖 <b>COMMANDS</b>",
        DIVIDER,
        "<b>General</b>",
        "/start — open the menu (and resume alerts after /stop)",
        "/stop — stop receiving alerts until you send /start",
        "/help — this list",
        "/status — monitoring status",
        "",
        "<b>Whale data</b>",
        "/whales — recent whale events",
        "/recent — latest signals",
        "/orders — large resting orders",
        "/positions — tracked open positions",
        "/coins — monitored coins",
    ]
    if admin:
        lines += [
            "",
            "<b>Monitoring</b>",
            "/startmonitor — resume alerts",
            "/stopmonitor — pause alerts",
            "",
            "<b>Filters</b>",
            "/threshold — show the whale filter",
            "/setthreshold &lt;USD&gt; — e.g. /setthreshold 2000000",
            "/margin — show the margin gate",
            "/setmargin &lt;USD&gt; — alert only above this margin (0 = off)",
            "/cooldown &lt;seconds&gt; — per-signal cooldown",
            "/setcoins BTC ETH SOL — <b>replace</b> the whole coin list",
            "/addcoin XRP — add a coin, keeping the rest",
            "/removecoin SOL — remove one coin",
            "/allcoins on|off — monitor every coin",
            "",
            "<b>Wallets</b>",
            "/watch &lt;0x…&gt; — always enrich this wallet",
            "/unwatch &lt;0x…&gt; — stop watching",
            "/wallets — watched and top-scoring wallets",
            "",
            "<b>Access</b>",
            "/public on|off — public or private mode",
            "/admins — list administrators",
            "",
            "<b>Diagnostics</b>",
            "/stats — system statistics",
            "/config — the stored configuration",
            "/panel — control panel",
        ]
    if main_admin:
        lines += [
            "",
            "<b>Main Admin only</b>",
            "/addadmin &lt;user id&gt; — add a Co-Admin",
            "/removeadmin &lt;user id&gt; — remove a Co-Admin",
            "/audit — recent admin actions",
            "/resetsettings — reset every setting (asks first)",
        ]
    return "\n".join(lines)


# ── whale filter (spec §8) ─────────────────────────────────────
def threshold_panel(config: RuntimeConfig) -> str:
    lines = [
        "🐋 <b>WHALE FILTER</b>",
        DIVIDER,
        "<b>Minimum Value:</b>",
        fmt_usd_full(config.min_whale_value),
        "<b>Coins:</b>",
        escape_html(config.coin_label),
        "<b>Status:</b>",
        "🟢 ACTIVE" if config.monitoring_enabled else "🔴 PAUSED",
    ]
    overrides = {
        "Large trades": config.min_trade_value,
        "Position notional": config.min_position_value,
        "Position change": config.min_position_delta_value,
        "Resting orders": config.min_order_value,
    }
    extra = [f"• {label}: {fmt_usd_full(value)}" for label, value in overrides.items() if value]
    if extra:
        lines += [DIVIDER, "<b>Per-type overrides</b>", *extra]
    lines += [
        DIVIDER,
        f"⏱ Cooldown: {config.alert_cooldown_seconds}s per repeated signal",
    ]
    return "\n".join(lines)


def margin_panel(config: RuntimeConfig) -> str:
    """Explain exactly what the margin gate does, including what it cannot do."""
    lines = [
        "🏦 <b>MARGIN FILTER</b>",
        DIVIDER,
        "<b>Minimum margin:</b>",
        fmt_usd_full(config.min_margin_value) if config.margin_gate_enabled else "🚫 Off",
    ]
    if config.margin_gate_enabled:
        lines += [
            DIVIDER,
            "Only positions with at least this much <b>collateral at risk</b>",
            "(Hyperliquid's <code>marginUsed</code>) will alert.",
            "",
            "This is <b>not</b> the position notional: a $20M position at 20x",
            "carries $1M of margin.",
            "",
            "⚠️ Resting-order and order-book signals have no margin —",
            "no collateral is committed until a fill — so they are not",
            "gated by this setting. Use 💰 Threshold for those.",
            "",
            "⚠️ A position whose margin cannot be read is <b>not</b> sent.",
            "See /status if signals go quiet.",
        ]
    else:
        lines += [
            DIVIDER,
            "The margin gate is off: signals are filtered by value only.",
            "Turn it on to receive alerts only from positions holding at",
            "least a given amount of collateral at risk.",
        ]
    lines += [
        DIVIDER,
        f"💰 Minimum value: {fmt_usd_full(config.min_whale_value)}",
    ]
    return "\n".join(lines)


# ── coins (spec §7) ────────────────────────────────────────────
def coins_panel(config: RuntimeConfig, monitored: Sequence[str] = (), skipped: int = 0) -> str:
    lines = [
        "🪙 <b>MONITORED COINS</b>",
        DIVIDER,
        f"<b>Mode:</b> {'ALL COINS' if config.all_coins else 'SELECTED COINS'}",
    ]
    if config.all_coins:
        live = ", ".join(monitored[:40]) if monitored else "awaiting Hyperliquid universe"
        lines.append(f"<b>Live subscriptions:</b> {escape_html(live)}")
        if skipped:
            lines.append(f"<i>{skipped} lower-volume coins not subscribed.</i>")
    else:
        lines.append(
            f"<b>Selected:</b> {escape_html(', '.join(config.coins) if config.coins else 'none')}"
        )
        if monitored:
            lines.append(f"<b>Active feeds:</b> {escape_html(', '.join(monitored))}")
    lines += [
        DIVIDER,
        "Add: /addcoin XRP · Remove: /removecoin SOL",
        "Replace the whole list: /setcoins BTC ETH SOL",
    ]
    return "\n".join(lines)


# ── monitoring status (spec §10) ───────────────────────────────
def status_panel(config: RuntimeConfig, stats: Mapping[str, Any]) -> str:
    engine = stats.get("engine") or {}
    market = engine.get("market_ws") or {}
    rest = engine.get("rest") or {}
    alerts = stats.get("alerts") or {}
    filt = engine.get("filter") or {}

    connected = bool(market.get("connected"))
    lines = [
        "📡 <b>MONITORING STATUS</b>",
        DIVIDER,
    ]
    if config.paused:
        # First line, before anything else: while paused every figure below is
        # frozen, and a reader who misses that will read "Monitoring: ON, feed
        # disconnected" as a fault.
        lines += [
            "⏸️ <b>GLOBALLY PAUSED</b> — send <code>/go</code> to resume.",
            "<i>Feeds are down and alerts are withheld by choice, not by fault. "
            "The counters below are frozen where the pause found them.</i>",
            DIVIDER,
        ]
    lines += [
        f"<b>Monitoring:</b> {'🟢 ON' if config.monitoring_enabled else '🔴 OFF'}"
        + ("  <i>(overridden by pause)</i>" if config.paused else ""),
        f"<b>Hyperliquid feed:</b> {'🟢 connected' if connected else '🔴 disconnected'}",
        f"<b>Access:</b> {'🌐 PUBLIC' if config.public_mode else '🔒 PRIVATE'}",
        f"<b>Threshold:</b> {fmt_usd_full(config.min_whale_value)}",
        "<b>Margin gate:</b> "
        + (fmt_usd_full(config.min_margin_value) if config.margin_gate_enabled else "off"),
        f"<b>Coins:</b> {escape_html(config.coin_label)}",
        DIVIDER,
        f"Trades observed: <b>{engine.get('trades_seen', 0):,}</b>",
        f"Whale events: <b>{engine.get('events_detected', 0):,}</b>",
        f"Alerts sent: <b>{alerts.get('sent', 0):,}</b>",
        f"Wallets tracked: <b>{(engine.get('tracker') or {}).get('tracked', 0):,}</b>",
        f"Live order streams: <b>{len(engine.get('focus_wallets') or [])}"
        f"/{engine.get('focus_cap', 0)}</b>",
    ]

    if alerts.get("paused_drops"):
        lines.append(f"Alerts withheld while paused: <b>{alerts['paused_drops']:,}</b>")
    if alerts.get("no_recipients"):
        # Issue 8: "0 delivered" must never be a silent state.
        lines.append(
            f"⚠️ Alerts with no recipient: <b>{alerts['no_recipients']:,}</b>"
            " — every admin is unsubscribed (/start to re-subscribe) or has never "
            "opened a chat with the bot."
        )

    reconnects = market.get("reconnects")
    if reconnects:
        lines.append(f"Feed reconnects: <b>{reconnects}</b>")
    if rest.get("last_error"):
        lines.append(f"⚠️ Last REST error: {escape_html(str(rest['last_error'])[:80])}")
    if not connected and market.get("last_error"):
        lines.append(f"⚠️ Feed error: {escape_html(str(market['last_error'])[:80])}")

    suppressed = filt.get("rejected") or {}
    if suppressed:
        reasons = ", ".join(f"{key}: {value:,}" for key, value in sorted(suppressed.items()))
        lines += [DIVIDER, f"<i>Filtered: {escape_html(reasons)}</i>"]

    last_event = engine.get("last_event_at")
    lines += [
        DIVIDER,
        f"Uptime: <b>{fmt_duration(stats.get('uptime_seconds') or 0)}</b>",
        f"Last whale event: <b>{_ago_or_never(last_event)}</b>",
    ]
    return "\n".join(lines)


def monitoring_started() -> str:
    return "🟢 <b>Monitoring started.</b>\nWhale alerts are live."


def monitoring_stopped() -> str:
    return (
        "🔴 <b>Monitoring stopped.</b>\n"
        "Alerts are paused. Hyperliquid data is still being read so state stays "
        "current, and nothing already recorded is lost."
    )


# ── global pause / resume ──────────────────────────────────────
def paused_confirmation() -> str:
    """What /pause reports back.

    Deliberately spells out that this is wider than /stopmonitor, because the two
    are easy to confuse and only one of them stops the bot answering commands.
    """
    return "\n".join(
        [
            "⏸️ <b>BOT PAUSED</b>",
            DIVIDER,
            "Everything is stopped:",
            "• Hyperliquid feeds disconnected — no trades, positions, orders or books",
            "• no detection, no alerts (anything detected meanwhile is discarded)",
            "• every command except <code>/go</code> is refused",
            DIVIDER,
            "Nothing is lost. Settings, coins, watched wallets, admins and recorded "
            "history are untouched, and the pause survives a redeploy.",
            "Send <code>/go</code> to resume.",
        ]
    )


def resumed_confirmation(was_paused: bool) -> str:
    """What /go reports back. ``was_paused`` is False if it was already running."""
    if not was_paused:
        return "▶️ <b>Already running.</b>\nThe bot was not paused; nothing changed."
    return "\n".join(
        [
            "▶️ <b>BOT RESUMED</b>",
            DIVIDER,
            "Feeds are reconnecting and commands are open again. The monitoring "
            "switch is whatever it was before the pause — the status below is "
            "authoritative.",
        ]
    )


def paused_notice(*, admin: bool = False) -> str:
    """The refusal every non-exempt handler gets while paused.

    An admin is told how to lift it; a normal user is not, because /go is an
    admin control and naming it would advertise a privileged command (issue 5).
    """
    if admin:
        return (
            "⏸️ <b>The bot is paused.</b>\n"
            "Send <code>/go</code> to resume. Only <code>/go</code> works until then."
        )
    return "⏸️ <b>The bot is paused.</b>\nAn administrator has stopped it. Try again later."


# ── public mode (spec §11) ─────────────────────────────────────
def public_mode_panel(config: RuntimeConfig, subscribers: int) -> str:
    return "\n".join(
        [
            "🌐 <b>PUBLIC MODE</b>",
            DIVIDER,
            f"<b>Current:</b> {'🌐 PUBLIC' if config.public_mode else '🔒 PRIVATE'}",
            "",
            "<b>PUBLIC</b> — anyone who starts the bot can view signals and",
            "receives alerts. Administration stays admin-only.",
            "<b>PRIVATE</b> — only administrators can use the bot.",
            DIVIDER,
            f"Registered users: <b>{subscribers:,}</b>",
            "Commands: /public on · /public off",
        ]
    )


# ── admins (spec §13–§15) ──────────────────────────────────────
def admin_list(entries: Sequence[Mapping[str, Any]], main_admin_id: int) -> str:
    lines = ["👥 <b>ADMIN MANAGEMENT</b>", DIVIDER]
    co_admins = 0
    for entry in entries:
        telegram_id = entry["telegram_id"]
        is_main = entry["role"] == "MAIN_ADMIN"
        if not is_main:
            co_admins += 1
        label = "👑 Main Admin" if is_main else "🛡 Co-Admin"
        username = entry.get("username")
        handle = f" (@{escape_html(str(username))})" if username else ""
        lines.append(f"{label} — <code>{telegram_id}</code>{handle}")
    lines += [
        DIVIDER,
        f"Co-Admins: <b>{co_admins}</b> (no limit)",
        "",
        "The Main Admin is set by <code>MAIN_ADMIN_ID</code> in the deployment",
        "environment and cannot be changed, removed or transferred from Telegram.",
        "Co-Admins cannot add or remove administrators.",
    ]
    return "\n".join(lines)


def admin_roster(entries: Sequence[Mapping[str, Any]], main_admin_id: int) -> str:
    """The 📋 List Co-Admins panel.

    Deliberately *not* :func:`admin_list`. The button lives on the admin panel,
    which already renders ``admin_list``; answering it with the same text made
    Telegram reject the edit as "message is not modified", so the button looked
    dead. This view carries what the roster is actually for — who granted each
    Co-Admin, and when.
    """
    co_admins = [e for e in entries if e.get("role") != "MAIN_ADMIN"]
    lines = ["📋 <b>CO-ADMIN ROSTER</b>", DIVIDER]
    main = next((e for e in entries if e.get("role") == "MAIN_ADMIN"), None)
    main_id = main["telegram_id"] if main else main_admin_id
    lines.append(f"👑 <b>Main Admin</b> — <code>{main_id}</code>")
    if main and main.get("username"):
        lines.append(f"   @{escape_html(str(main['username']))}")
    lines.append("")
    if not co_admins:
        lines.append("No Co-Admin has been added yet.")
    for index, entry in enumerate(co_admins, start=1):
        telegram_id = entry["telegram_id"]
        username = entry.get("username")
        lines.append(f"🛡 <b>Co-Admin {index}</b> — <code>{telegram_id}</code>")
        if username:
            lines.append(f"   @{escape_html(str(username))}")
        added_by = entry.get("added_by")
        if added_by:
            lines.append(f"   added by <code>{added_by}</code>")
        created_at = entry.get("created_at")
        if created_at:
            lines.append(f"   added {fmt_time(created_at)} ({fmt_ago(created_at)})")
    lines += [
        DIVIDER,
        f"Co-Admins: <b>{len(co_admins)}</b> (no limit)",
        "A Co-Admin cannot add, remove or promote administrators.",
    ]
    return "\n".join(lines)


def co_admin_added(telegram_id: int) -> str:
    """Spec §15 confirmation."""
    return "\n".join(
        [
            "✅ <b>Co-Admin Added</b>",
            "<b>User ID:</b>",
            f"<code>{telegram_id}</code>",
            "<b>Role:</b>",
            "Co-Admin",
        ]
    )


def co_admin_removed(telegram_id: int) -> str:
    return "\n".join(
        [
            "✅ <b>Co-Admin Removed</b>",
            "<b>User ID:</b>",
            f"<code>{telegram_id}</code>",
            "<b>Role:</b>",
            "User",
        ]
    )


def promoted_notice() -> str:
    return (
        "🛡 <b>You are now a Co-Admin</b> of the Hyperliquid whale monitor.\n"
        "Send /start to open the control panel."
    )


def demoted_notice() -> str:
    return "ℹ️ Your Co-Admin access to the Hyperliquid whale monitor has been removed."


# ── settings (spec §27) ────────────────────────────────────────
def _threshold_overrides(config: RuntimeConfig) -> str | None:
    """Name the per-class gates only when one actually differs from the base."""
    labels = {
        "trade": "trades",
        "position": "positions",
        "position_delta": "position changes",
        "order": "orders",
    }
    base = float(config.min_whale_value)
    differing = [
        f"{labels[name]} {fmt_usd_full(value)}"
        for name, value in config.effective_thresholds.items()
        if abs(value - base) > 0.005
    ]
    return " · ".join(differing) if differing else None


def settings_panel(config: RuntimeConfig) -> str:
    overrides = _threshold_overrides(config)
    return "\n".join(
        [
            "⚙️ <b>SETTINGS</b>",
            DIVIDER,
            *(
                ["⏸️ <b>Globally paused</b> — <code>/go</code> to resume.", DIVIDER]
                if config.paused
                else []
            ),
            f"💰 <b>Minimum Whale Value:</b> {fmt_usd_full(config.min_whale_value)}"
            " <i>(applies at ≥)</i>",
            *([f"   ↳ per-class overrides: {overrides}"] if overrides else []),
            "🏦 <b>Minimum Margin:</b> "
            + (fmt_usd_full(config.min_margin_value) if config.margin_gate_enabled else "off"),
            f"🪙 <b>Monitored Coins:</b> {escape_html(config.coin_label)}",
            f"📡 <b>Monitoring:</b> {'ON' if config.monitoring_enabled else 'OFF'}",
            f"🌐 <b>Public Mode:</b> {'ON' if config.public_mode else 'OFF'}",
            f"🔔 <b>Alert Settings:</b> {_detector_summary(config)}",
            f"⏱ <b>Cooldown:</b> {config.alert_cooldown_seconds}s",
            "👥 <b>Admins</b>",
            "📊 <b>Statistics</b>",
            DIVIDER,
            "Every setting is available as a command too — see /help.",
        ]
    )


def alert_settings(config: RuntimeConfig) -> str:
    rows = [
        ("Large trades", config.enable_trade_detector),
        ("Position changes", config.enable_position_detector),
        ("Resting orders", config.enable_order_detector),
        ("Order cancellations", config.enable_order_cancel_alerts),
        ("Wallet tracking", config.enable_wallet_tracking),
        ("Order book levels", config.enable_book_scanner),
    ]
    lines = ["🔔 <b>ALERT SETTINGS</b>", DIVIDER]
    lines += [f"{'🟢' if value else '🔴'} {label}" for label, value in rows]
    lines += [
        DIVIDER,
        "<i>Order book levels are aggregated by Hyperliquid and carry no wallet",
        "address, so those alerts never name a trader.</i>",
    ]
    return "\n".join(lines)


def _detector_summary(config: RuntimeConfig) -> str:
    enabled = sum(
        (
            config.enable_trade_detector,
            config.enable_position_detector,
            config.enable_order_detector,
            config.enable_order_cancel_alerts,
            config.enable_book_scanner,
        )
    )
    return f"{enabled}/5 detectors on"


# ── statistics (spec §32) ──────────────────────────────────────
def statistics_panel(summary: Mapping[str, Any], stats: Mapping[str, Any]) -> str:
    engine = stats.get("engine") or {}
    lines = [
        "📊 <b>SYSTEM STATISTICS</b>",
        DIVIDER,
        f"<b>Whale Events:</b> {summary.get('total', 0):,}",
        f"<b>LONG:</b> {summary.get('longs', 0):,}",
        f"<b>SHORT:</b> {summary.get('shorts', 0):,}",
        f"<b>Total Notional:</b> {fmt_usd(summary.get('notional'))}",
        f"<b>Largest Event:</b> {fmt_usd(summary.get('largest'))}",
    ]

    by_coin: Sequence[Mapping[str, Any]] = summary.get("by_coin") or []
    if by_coin:
        lines.append(DIVIDER)
        lines.append("<b>Events by coin</b>")
        lines += [
            f"• {escape_html(str(entry.get('coin')))}: {int(entry.get('count', 0)):,}"
            f" · {fmt_usd(entry.get('notional'))}"
            for entry in by_coin[:12]
        ]

    by_type: Sequence[Mapping[str, Any]] = summary.get("by_type") or []
    if by_type:
        lines.append(DIVIDER)
        lines.append("<b>Events by type</b>")
        lines += [
            f"• {escape_html(str(entry.get('type', '')).replace('_', ' '))}: "
            f"{int(entry.get('count', 0)):,}"
            for entry in by_type[:10]
        ]

    lines += [
        DIVIDER,
        f"<b>Uptime:</b> {fmt_duration(stats.get('uptime_seconds') or 0)}",
        f"Trades observed: {engine.get('trades_seen', 0):,}",
        f"Alerts delivered: {(stats.get('alerts') or {}).get('sent', 0):,}",
    ]
    window = summary.get("window_label")
    if window:
        lines.append(f"<i>Counts cover {escape_html(str(window))}.</i>")
    return "\n".join(lines)


def diagnostics_panel(health: Mapping[str, Any], stats: Mapping[str, Any]) -> str:
    engine = stats.get("engine") or {}
    market = engine.get("market_ws") or {}
    rest = engine.get("rest") or {}
    dedup = engine.get("dedup") or {}
    alerts = stats.get("alerts") or {}
    database = health.get("database") or {}
    badge = {"healthy": "🟢", "degraded": "🟡", "unhealthy": "🔴"}.get(
        str(health.get("status")), "⚪"
    )
    lines = [
        "🩺 <b>DIAGNOSTICS</b>",
        DIVIDER,
        f"<b>Status:</b> {badge} {escape_html(str(health.get('status', 'unknown')).upper())}",
    ]
    for reason in health.get("reasons") or []:
        lines.append(f"• {escape_html(str(reason))}")
    lines += [
        DIVIDER,
        f"Database: {'🟢' if database.get('connected') else '🔴'}",
        f"Market feed: {'🟢' if market.get('connected') else '🔴'}"
        f" · subs {market.get('active', 0)}/{market.get('subscriptions', 0)}"
        f" · reconnects {market.get('reconnects', 0)}",
        f"REST: {rest.get('requests', 0):,} requests · {rest.get('errors', 0)} errors"
        f" · weight {rest.get('weight_spent_last_minute', 0)}/{rest.get('weight_budget', 0)}",
        f"Queue: {engine.get('queue_depth', 0)} pending · {engine.get('queue_dropped', 0)} dropped",
        f"Dedup: {dedup.get('suppressed', 0):,} suppressed",
        f"Alerts: {alerts.get('sent', 0):,} sent · {alerts.get('failed', 0)} failed"
        f" · {alerts.get('throttled', 0)} throttled",
    ]
    for warning in health.get("warnings") or []:
        lines.append(f"⚠️ {escape_html(str(warning))}")
    return "\n".join(lines)


# ── list views (spec §12 / §20 / §28) ──────────────────────────
def _threshold_footer(config: RuntimeConfig, *classes: str) -> list[str]:
    """Explain, per event class, the gate a row had to pass.

    Task "ADMIN UI + DATA INTEGRITY" issue 7: a single ``Threshold:`` line over
    a list of historical rows is ambiguous in two ways at once — the thresholds
    are *per class* (an order can have a lower gate than a position), and the
    rows were recorded under whatever threshold was in force at the time, which
    an admin may have raised since. Both are stated instead of assumed.
    """
    labels = {
        "trade": "executed trades",
        "position": "positions",
        "position_delta": "position changes",
        "order": "resting orders",
    }
    effective = config.effective_thresholds
    shown = [c for c in classes if c in effective] or list(effective)
    lines = ["<b>Alert thresholds</b> (an event alerts at <b>≥</b> its class threshold):"]
    lines += [
        f"• {labels.get(name, name)} {fmt_usd_full(effective[name])}"
        for name in shown
    ]
    if config.margin_gate_enabled:
        lines.append(
            f"• plus margin at risk ≥ {fmt_usd_full(config.min_margin_value)} "
            "for events carrying a position"
        )
    lines.append(
        "<i>Rows above are historical: each passed the threshold in force when it "
        "was detected, which may be lower than the current one.</i>"
    )
    return lines


def whale_list(rows: Sequence[Any], config: RuntimeConfig) -> str:
    if not rows:
        return _empty(
            "🐋 <b>RECENT WHALES</b>",
            config,
            "No whale event has crossed the threshold yet.",
        )
    lines = ["🐋 <b>RECENT WHALES</b>", DIVIDER]
    for row in rows:
        side = (row.side or "").upper()
        badge = {"LONG": "📈", "SHORT": "📉", "BUY": "🟢", "SELL": "🔴"}.get(side, "•")
        lines.append(
            f"{badge} <b>{escape_html(row.coin)}</b> {escape_html(side)} "
            f"{fmt_usd(row.notional)} · {fmt_ago(row.event_time)}"
        )
        if row.wallet:
            # Complete address on its own line: long enough that sharing the
            # line with other detail would wrap unpredictably, and it is the
            # one field the reader needs to copy.
            lines.append(f"   👤 {wallet_code(row.wallet)}")
        detail = []
        if row.entry_px:
            detail.append(f"entry {fmt_price(row.entry_px)}")
        if row.leverage:
            detail.append(f"{row.leverage:g}x")
        if detail:
            lines.append("   " + " · ".join(detail))
    lines += [DIVIDER, *_threshold_footer(config)]
    return "\n".join(lines)


def order_list(rows: Sequence[Any], config: RuntimeConfig) -> str:
    if not rows:
        return _empty(
            "📋 <b>LARGE RESTING ORDERS</b>",
            config,
            "No resting order above the threshold is currently tracked.",
        )
    lines = ["📋 <b>LARGE RESTING ORDERS</b>", DIVIDER]
    for row in rows:
        side = (row.side or "").upper()
        badge = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "•"
        lines.append(
            f"{badge} <b>{escape_html(row.coin)}</b> {escape_html(side)} "
            f"{fmt_usd(row.notional)} @ {fmt_price(row.limit_px)}"
        )
        if row.wallet:
            lines.append(f"   👤 {wallet_code(row.wallet)}")
        detail = [f"status {escape_html(str(row.status or 'open'))}"]
        if row.placed_at:
            detail.append(f"placed {fmt_ago(row.placed_at)}")
        lines.append("   " + " · ".join(detail))
    lines += [
        DIVIDER,
        "<i>Resting orders only — an order is not a position. Hyperliquid does",
        "not publish a global feed of every order, so this covers wallets the",
        "monitor is enriching.</i>",
        *_threshold_footer(config, "order"),
    ]
    return "\n".join(lines)


def position_list(rows: Sequence[Any], config: RuntimeConfig) -> str:
    if not rows:
        return _empty(
            "📈 <b>TRACKED POSITIONS</b>",
            config,
            "No whale position is currently tracked.",
        )
    lines = ["📈 <b>TRACKED POSITIONS</b>", DIVIDER]
    for row in rows:
        side = (row.side or "").upper()
        badge = "📈" if side == "LONG" else "📉" if side == "SHORT" else "•"
        lines.append(
            f"{badge} <b>{escape_html(row.coin)}</b> {escape_html(side)} "
            f"{fmt_usd(row.position_value)}"
        )
        if row.wallet:
            lines.append(f"   👤 {wallet_code(row.wallet)}")
        detail = []
        if row.entry_px:
            detail.append(f"entry {fmt_price(row.entry_px)}")
        if row.leverage:
            detail.append(f"{row.leverage:g}x")
        if row.liquidation_px:
            detail.append(f"liq {fmt_price(row.liquidation_px)}")
        if detail:
            lines.append("   " + " · ".join(detail))
        if row.position_value is None or side not in ("LONG", "SHORT"):
            # Never invent a notional. A row can only look like this if it was
            # written before a clearinghouseState snapshot confirmed the
            # position — say so rather than dressing it up.
            lines.append(
                "   ℹ️ no confirmed clearinghouseState snapshot for this row — "
                "notional unverified"
            )
    lines += [
        DIVIDER,
        "<i>Snapshots from clearinghouseState for wallets under observation.",
        "Liquidation prices are Hyperliquid's own figures; blank means the",
        "exchange did not publish one.</i>",
    ]
    return "\n".join(lines)


def wallet_list(tracked: Sequence[str], top: Sequence[Mapping[str, Any]]) -> str:
    """Spec §20 — activity only, never an identity claim."""
    lines = ["🐋 <b>WHALE WALLETS</b>", DIVIDER]
    if tracked:
        lines.append("<b>Watched</b>")
        lines += [f"• {wallet_code(address)}" for address in tracked]
        lines.append("")
    if top:
        lines.append("<b>Most active recently</b>")
        for entry in top:
            coins = ", ".join(str(coin) for coin in (entry.get("coins") or [])[:3])
            lines.append(
                f"• {wallet_code(str(entry.get('address')))} — "
                f"{entry.get('trades', 0)} trades · {fmt_usd(entry.get('volume'))}"
                + (f" · {escape_html(coins)}" if coins else "")
            )
            position_value = entry.get("position_value")
            if position_value:
                lines.append(f"    open positions {fmt_usd(position_value)}")
    if not tracked and not top:
        lines.append("No wallet has crossed the threshold yet.")
    lines += [
        DIVIDER,
        "<i>Addresses only. No wallet is attributed to a person or company.</i>",
        "Commands: /watch &lt;0x…&gt; · /unwatch &lt;0x…&gt;",
    ]
    return "\n".join(lines)


def live_signals(rows: Sequence[Any], config: RuntimeConfig, connected: bool) -> str:
    header = [
        "📡 <b>LIVE SIGNALS</b>",
        DIVIDER,
        f"Feed: {'🟢 connected' if connected else '🔴 reconnecting'}",
        f"Monitoring: {'🟢 ON' if config.monitoring_enabled else '🔴 OFF'}",
        f"Threshold: {fmt_usd_full(config.min_whale_value)}",
        f"Coins: {escape_html(config.coin_label)}",
        DIVIDER,
    ]
    if not rows:
        return "\n".join(
            header
            + [
                "No signal yet. Alerts arrive here automatically as soon as a",
                "whale-sized event is detected.",
            ]
        )
    body = []
    for row in rows[:8]:
        side = (row.side or "").upper()
        body.append(
            f"• <b>{escape_html(row.coin)}</b> {escape_html(side)} {fmt_usd(row.notional)}"
            f" — {fmt_time(row.event_time)}"
        )
    return "\n".join(header + ["<b>Latest</b>", *body])


def audit_list(rows: Sequence[Any]) -> str:
    if not rows:
        return "🧾 <b>AUDIT LOG</b>\n" + DIVIDER + "\nNo administrative action recorded yet."
    lines = ["🧾 <b>AUDIT LOG</b>", DIVIDER]
    for row in rows:
        lines.append(
            f"• <code>{row.admin_id}</code> {escape_html(row.action)}"
            f" {escape_html(str(row.target or ''))}"
        )
        if row.old_value is not None or row.new_value is not None:
            lines.append(
                f"    {escape_html(str(row.old_value))} → {escape_html(str(row.new_value))}"
            )
        lines.append(f"    {fmt_time(row.created_at)} · {fmt_ago(row.created_at)}")
    return "\n".join(lines)


def _empty(title: str, config: RuntimeConfig, note: str) -> str:
    return "\n".join(
        [
            title,
            DIVIDER,
            note,
            "",
            f"Threshold: <b>{fmt_usd_full(config.min_whale_value)}</b>",
            f"Coins: <b>{escape_html(config.coin_label)}</b>",
            f"Monitoring: {'🟢 ON' if config.monitoring_enabled else '🔴 OFF'}",
        ]
    )


def _ago_or_never(iso_timestamp: Any) -> str:
    if not iso_timestamp:
        return "never"
    from datetime import datetime

    try:
        return fmt_ago(datetime.fromisoformat(str(iso_timestamp)))
    except (TypeError, ValueError):
        return "unknown"


# ── validation feedback ────────────────────────────────────────
def invalid_number(argument: str, example: str) -> str:
    return (
        f"❌ <code>{escape_html(argument)}</code> is not a valid number.\n"
        f"Example: <code>{escape_html(example)}</code>"
    )


def invalid_user_id(argument: str) -> str:
    return (
        f"❌ <code>{escape_html(argument)}</code> is not a Telegram user ID.\n"
        "Send the numeric ID — @userinfobot will report it. Usernames can be "
        "changed, so the numeric ID is what gets stored."
    )


def invalid_coin(argument: str) -> str:
    return (
        f"❌ <code>{escape_html(argument)}</code> is not a valid coin symbol.\n"
        "Use the Hyperliquid ticker, e.g. <code>BTC</code>."
    )


def unknown_coin(argument: str) -> str:
    return (
        f"⚠️ <code>{escape_html(argument)}</code> is not in Hyperliquid's perpetual "
        "universe, so it was not added."
    )


def invalid_wallet(argument: str) -> str:
    return (
        f"❌ <code>{escape_html(argument)}</code> is not an EVM address.\n"
        "Expected 0x followed by 40 hex characters."
    )


def threshold_updated(value: float, clamped: bool) -> str:
    text = f"✅ Minimum whale value set to <b>{fmt_usd_full(value)}</b>."
    if clamped:
        text += "\n<i>Adjusted to the allowed range ($1,000 – $1,000,000,000).</i>"
    return text


def margin_updated(value: float, clamped: bool) -> str:
    if value <= 0:
        return "✅ Margin gate <b>off</b>. Signals are filtered by value only."
    text = (
        f"✅ Minimum margin set to <b>{fmt_usd_full(value)}</b>.\n"
        "Only positions with at least that much collateral at risk will alert."
    )
    if clamped:
        text += "\n<i>Adjusted to the allowed range ($1,000 – $1,000,000,000).</i>"
    return text


def cooldown_updated(seconds: int) -> str:
    if seconds == 0:
        return "✅ Alert cooldown disabled. Repeated signals will not be suppressed."
    return f"✅ Alert cooldown set to <b>{seconds}s</b> per repeated signal."


def coins_updated(config: RuntimeConfig) -> str:
    return f"✅ Monitored coins: <b>{escape_html(config.coin_label)}</b>"


def coins_added(
    config: RuntimeConfig, added: Sequence[str], already: Sequence[str] = ()
) -> str:
    """Report an *additive* change, naming what was added and what already was.

    Saying "already monitored" out loud matters: repeating /addcoin HYPE must be
    a no-op with a clear answer, not a silent success that leaves the admin
    wondering whether a duplicate was created (spec §3).
    """
    lines: list[str] = []
    if added:
        lines.append(f"✅ Added: <b>{escape_html(' '.join(added))}</b>")
    for coin in already:
        lines.append(f"ℹ️ <code>{escape_html(coin)}</code> was already monitored.")
    if not lines:
        lines.append("ℹ️ Nothing to add.")
    lines.append(f"🪙 Monitored coins: <b>{escape_html(config.coin_label)}</b>")
    return "\n".join(lines)


def coins_replaced(
    config: RuntimeConfig, added: Sequence[str], removed: Sequence[str]
) -> str:
    """Report an explicit whole-list replacement, including what it removed.

    /setcoins is allowed to remove, but never quietly: a removal an admin did not
    intend must be visible in the reply rather than discovered later by a missing
    alert (spec §22).
    """
    lines = [f"✅ Monitored coins: <b>{escape_html(config.coin_label)}</b>"]
    if added:
        lines.append(f"➕ Added: <b>{escape_html(' '.join(added))}</b>")
    if removed:
        lines.append(f"➖ Removed: <b>{escape_html(' '.join(removed))}</b>")
        lines.append("<i>/setcoins replaces the list. Use /addcoin to add without removing.</i>")
    if not added and not removed:
        lines.append("<i>No change — the list already matched.</i>")
    return "\n".join(lines)


def confirm_clear_coins(config: RuntimeConfig) -> str:
    return "\n".join(
        [
            "⚠️ <b>Clear the coin list?</b>",
            "",
            f"This removes all {len(config.coins)} monitored coin(s):",
            f"<b>{escape_html(config.coin_label)}</b>",
            "",
            "With an empty list and ALL COINS off, <b>nothing will be monitored</b>.",
            "This cannot be undone — the coins would have to be added again.",
        ]
    )


def coins_cleared(removed: Sequence[str]) -> str:
    if not removed:
        return "ℹ️ The coin list was already empty."
    return "\n".join(
        [
            f"🧹 Cleared <b>{len(removed)}</b> coin(s): {escape_html(' '.join(removed))}",
            "",
            "⚠️ Nothing is being monitored now. Add coins to resume alerts.",
        ]
    )


def confirm_reset_settings(config: RuntimeConfig) -> str:
    return "\n".join(
        [
            "⚠️ <b>Reset all settings to defaults?</b>",
            "",
            "This replaces every setting you have changed:",
            f"• Threshold — currently <b>{fmt_usd_full(config.min_whale_value)}</b>",
            f"• Coins — currently <b>{escape_html(config.coin_label)}</b>",
            f"• Mode — currently <b>{'PUBLIC' if config.public_mode else 'PRIVATE'}</b>",
            "• All alert toggles, the margin gate and the cooldown",
            "",
            "<b>Kept:</b> admins and co-admins, users, watched wallets, and",
            "recorded whale history. Only settings are reset.",
            "",
            "This cannot be undone.",
        ]
    )


def settings_reset(config: RuntimeConfig) -> str:
    return "\n".join(
        [
            "♻️ <b>Settings reset to defaults.</b>",
            "",
            f"Threshold: <b>{fmt_usd_full(config.min_whale_value)}</b>",
            f"Coins: <b>{escape_html(config.coin_label)}</b>",
            f"Mode: <b>{'PUBLIC' if config.public_mode else 'PRIVATE'}</b>",
            "",
            "Admins, users, watched wallets and whale history were kept.",
        ]
    )


def _same_value(live: Any, stored: Any) -> bool:
    """Compare a cached field against its stored row, tolerating float noise."""
    if isinstance(live, bool) or isinstance(stored, bool):
        return bool(live) == bool(stored)
    if isinstance(live, (int, float)) and isinstance(stored, (int, float)):
        return abs(float(live) - float(stored)) < 1e-6
    return live == stored


def config_snapshot(
    *,
    values: Mapping[str, Any],
    coins: Sequence[str],
    admins: Sequence[Mapping[str, Any]],
    wallets: Sequence[str],
    subscribers: int,
    cached: RuntimeConfig,
    durable: bool,
    bootstrapped_at: str | None = None,
) -> str:
    """``/config`` — what the database actually holds (spec §27).

    Every number here comes from a table read, so the panel cannot confirm itself:
    if the running cache has drifted from the stored rows, this is where it shows.
    No connection string, token or credential appears — only the backend kind.
    """
    field_names = {f.name for f in fields(cached)}
    drift = [
        key
        for key, stored in values.items()
        if key in field_names and not _same_value(getattr(cached, key), stored)
    ]
    stored_coins = tuple(sorted({c.upper() for c in coins}))
    if stored_coins != cached.coins:
        drift.append("coins")

    main = [a for a in admins if str(a.get("role", "")).upper().startswith("MAIN")]
    co = [a for a in admins if a not in main]

    def stored_bool(key: str, default: bool) -> str:
        raw = values.get(key, default)
        return "ON" if bool(raw) else "OFF"

    threshold = values.get("min_whale_value", cached.min_whale_value)
    margin = float(values.get("min_margin_value") or 0.0)
    lines = [
        "🗄 <b>PERSISTENT CONFIGURATION</b>",
        DIVIDER,
        f"Storage: <b>{'PostgreSQL' if durable else 'SQLite'}</b>"
        + ("" if durable else " — <b>local file, not durable on Railway</b>"),
        f"Stored setting rows: <b>{len(values)}</b>",
    ]
    if bootstrapped_at:
        lines.append(f"Initialised: <code>{escape_html(str(bootstrapped_at))}</code>")
    lines += [
        "",
        "<i>Read directly from the database, not from the running cache.</i>",
        "",
        f"💵 Threshold: <b>{fmt_usd_full(float(threshold or 0.0))}</b>",
        f"🛡 Margin gate: <b>{fmt_usd_full(margin) if margin > 0 else 'off'}</b>",
        f"⏱ Cooldown: <b>{int(values.get('alert_cooldown_seconds', 0) or 0)}s</b>",
        f"🪙 Coins ({len(stored_coins)}): "
        + (f"<b>{escape_html(' '.join(stored_coins))}</b>" if stored_coins else "<b>none</b>"),
        f"   Selection: <b>{'ALL COINS' if values.get('all_coins') else 'SELECTED'}</b>",
        f"🌐 Mode: <b>{'PUBLIC' if values.get('public_mode') else 'PRIVATE'}</b>",
        f"📡 Monitoring: <b>{stored_bool('monitoring_enabled', True)}</b>"
        f" · Paused: <b>{'YES' if values.get('paused') else 'no'}</b>",
        f"🔔 Trades: <b>{stored_bool('enable_trade_detector', True)}</b>"
        f" · Positions: <b>{stored_bool('enable_position_detector', True)}</b>"
        f" · Orders: <b>{stored_bool('enable_order_alerts', False)}</b>",
        f"👑 Admins: <b>{len(main)}</b> main, <b>{len(co)}</b> co-admin",
        f"👛 Watched wallets: <b>{len(wallets)}</b>",
        f"👥 Alert subscribers: <b>{subscribers}</b>",
        "",
    ]
    if drift:
        lines.append(
            "⚠️ <b>Cache differs from the database:</b> "
            f"<code>{escape_html(', '.join(sorted(set(drift))))}</code>"
        )
        lines.append("Restart to reload from the database, then re-check.")
    else:
        lines.append("✅ The running configuration matches the database exactly.")
    if durable:
        lines.append("✅ These values survive a restart and a redeploy.")
    else:
        lines.append("⚠️ Set <code>DATABASE_URL</code> to Postgres for durable storage.")
    return "\n".join(lines)


def unknown_command() -> str:
    return "❓ Unknown command. Send /help for the list of available commands."


def error_notice() -> str:
    return (
        "⚠️ Something went wrong handling that. The error has been logged and the "
        "monitor is still running — please try again."
    )


def rate_limited() -> str:
    return "⏳ Too many requests. Please wait a moment and try again."
