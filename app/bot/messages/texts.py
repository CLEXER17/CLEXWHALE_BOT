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
    short_wallet,
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
PROMPT_COINS = (
    "Send the coins to monitor, separated by spaces.\n"
    "Example: <code>BTC ETH SOL</code>"
)
PROMPT_COOLDOWN = "Send the alert cooldown in seconds (0–3600)."
PROMPT_WALLET = "Send the wallet address to watch (0x…)."
CANCELLED = "✖️ Cancelled."


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
        "/start — open the menu",
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
            "/cooldown &lt;seconds&gt; — per-signal cooldown",
            "/setcoins BTC ETH SOL — replace the coin list",
            "/addcoin XRP — add one coin",
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
            "/panel — control panel",
        ]
    if main_admin:
        lines += [
            "",
            "<b>Main Admin only</b>",
            "/addadmin &lt;user id&gt; — add a Co-Admin",
            "/removeadmin &lt;user id&gt; — remove a Co-Admin",
            "/audit — recent admin actions",
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
        "Commands: /setcoins BTC ETH SOL · /addcoin XRP · /removecoin SOL",
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
        f"<b>Monitoring:</b> {'🟢 ON' if config.monitoring_enabled else '🔴 OFF'}",
        f"<b>Hyperliquid feed:</b> {'🟢 connected' if connected else '🔴 disconnected'}",
        f"<b>Access:</b> {'🌐 PUBLIC' if config.public_mode else '🔒 PRIVATE'}",
        f"<b>Threshold:</b> {fmt_usd_full(config.min_whale_value)}",
        f"<b>Coins:</b> {escape_html(config.coin_label)}",
        DIVIDER,
        f"Trades observed: <b>{engine.get('trades_seen', 0):,}</b>",
        f"Whale events: <b>{engine.get('events_detected', 0):,}</b>",
        f"Alerts sent: <b>{alerts.get('sent', 0):,}</b>",
        f"Wallets tracked: <b>{(engine.get('tracker') or {}).get('tracked', 0):,}</b>",
        f"Live order streams: <b>{len(engine.get('focus_wallets') or [])}"
        f"/{engine.get('focus_cap', 0)}</b>",
    ]

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
def settings_panel(config: RuntimeConfig) -> str:
    return "\n".join(
        [
            "⚙️ <b>SETTINGS</b>",
            DIVIDER,
            f"💰 <b>Minimum Whale Value:</b> {fmt_usd_full(config.min_whale_value)}",
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
        detail = []
        if row.wallet:
            detail.append(f"<code>{escape_html(short_wallet(row.wallet))}</code>")
        if row.entry_px:
            detail.append(f"entry {fmt_price(row.entry_px)}")
        if row.leverage:
            detail.append(f"{row.leverage:g}x")
        if detail:
            lines.append("   " + " · ".join(detail))
    lines += [DIVIDER, f"Threshold: {fmt_usd_full(config.min_whale_value)}"]
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
        detail = [f"status {escape_html(str(row.status or 'open'))}"]
        if row.wallet:
            detail.append(f"<code>{escape_html(short_wallet(row.wallet))}</code>")
        if row.placed_at:
            detail.append(f"placed {fmt_ago(row.placed_at)}")
        lines.append("   " + " · ".join(detail))
    lines += [
        DIVIDER,
        "<i>Resting orders only. Hyperliquid does not publish a global feed of",
        "every order, so this covers wallets the monitor is enriching.</i>",
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
        detail = []
        if row.wallet:
            detail.append(f"<code>{escape_html(short_wallet(row.wallet))}</code>")
        if row.entry_px:
            detail.append(f"entry {fmt_price(row.entry_px)}")
        if row.leverage:
            detail.append(f"{row.leverage:g}x")
        if row.liquidation_px:
            detail.append(f"liq {fmt_price(row.liquidation_px)}")
        if detail:
            lines.append("   " + " · ".join(detail))
    lines += [
        DIVIDER,
        "<i>Snapshots from clearinghouseState for wallets under observation.</i>",
    ]
    return "\n".join(lines)


def wallet_list(tracked: Sequence[str], top: Sequence[Mapping[str, Any]]) -> str:
    """Spec §20 — activity only, never an identity claim."""
    lines = ["🐋 <b>WHALE WALLETS</b>", DIVIDER]
    if tracked:
        lines.append("<b>Watched</b>")
        lines += [f"• <code>{escape_html(short_wallet(address))}</code>" for address in tracked]
        lines.append("")
    if top:
        lines.append("<b>Most active recently</b>")
        for entry in top:
            coins = ", ".join(str(coin) for coin in (entry.get("coins") or [])[:3])
            lines.append(
                f"• <code>{escape_html(short_wallet(str(entry.get('address'))))}</code> — "
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


def cooldown_updated(seconds: int) -> str:
    if seconds == 0:
        return "✅ Alert cooldown disabled. Repeated signals will not be suppressed."
    return f"✅ Alert cooldown set to <b>{seconds}s</b> per repeated signal."


def coins_updated(config: RuntimeConfig) -> str:
    return f"✅ Monitored coins: <b>{escape_html(config.coin_label)}</b>"


def unknown_command() -> str:
    return "❓ Unknown command. Send /help for the list of available commands."


def error_notice() -> str:
    return (
        "⚠️ Something went wrong handling that. The error has been logged and the "
        "monitor is still running — please try again."
    )


def rate_limited() -> str:
    return "⏳ Too many requests. Please wait a moment and try again."
