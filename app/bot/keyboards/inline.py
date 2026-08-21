"""Inline keyboards and the callback-data namespace.

Callback data is a compact ``area:action:arg`` string, well inside Telegram's
64-byte limit. It is treated as **untrusted input**: the router in
``app.bot.handlers.callbacks`` re-derives the caller's role from
``update.effective_user.id`` and checks the required capability before acting, so
hand-crafting ``admin:add`` in a client gets a refusal, not an escalation.
"""

from __future__ import annotations

from typing import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.settings_service import RuntimeConfig
from app.utils.formatting import bool_badge, fmt_usd

# ── callback namespaces ────────────────────────────────────────
CB_PANEL = "panel"
CB_MON = "mon"
CB_THRESH = "thr"
CB_MARGIN = "mgn"
CB_COIN = "coin"
CB_ADMIN = "adm"
CB_PUBLIC = "pub"
CB_SET = "set"
CB_STATS = "stats"
CB_DATA = "data"
CB_NOOP = "noop"

#: Common coins offered as quick-add buttons. These are only UI suggestions —
#: the live universe always comes from Hyperliquid's ``meta`` response.
SUGGESTED_COINS = ("BTC", "ETH", "SOL", "DOGE", "XRP", "HYPE", "SUI", "AVAX", "LINK", "ADA")

#: Threshold presets in USD.
THRESHOLD_PRESETS = (500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000)
#: Margin presets in USD. ``0`` is the "off" button, not a $0 gate.
MARGIN_PRESETS = (0, 100_000, 500_000, 1_000_000, 2_000_000, 5_000_000)
COOLDOWN_PRESETS = (0, 15, 30, 60, 300)


def _cb(*parts: object) -> str:
    return ":".join(str(part) for part in parts)


def back_button(target: str = CB_PANEL) -> InlineKeyboardButton:
    return InlineKeyboardButton("⬅️ Back", callback_data=_cb(target, "open"))


# ── admin control panel (spec §9) ──────────────────────────────
def control_panel(config: RuntimeConfig) -> InlineKeyboardMarkup:
    """The layout reflects live state: the monitoring button shows what *is*."""
    monitoring = "🟢 Monitoring: ON" if config.monitoring_enabled else "🔴 Monitoring: OFF"
    if config.paused:
        # While paused every other button is refused by the middleware, so the
        # panel offers the one action that still works instead of a wall of
        # buttons that answer "the bot is paused".
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("▶️ RESUME BOT", callback_data=_cb(CB_MON, "resume"))],
                [InlineKeyboardButton("📡 Status", callback_data=_cb(CB_MON, "status"))],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(monitoring, callback_data=_cb(CB_MON, "toggle")),
                InlineKeyboardButton("💰 Threshold", callback_data=_cb(CB_THRESH, "open")),
            ],
            [
                InlineKeyboardButton(
                    "🏦 Margin: "
                    + (fmt_usd(config.min_margin_value, 0) if config.margin_gate_enabled else "off"),
                    callback_data=_cb(CB_MARGIN, "open"),
                ),
            ],
            [
                InlineKeyboardButton("🪙 Coins", callback_data=_cb(CB_COIN, "open")),
                InlineKeyboardButton("📊 Signals", callback_data=_cb(CB_DATA, "whales")),
                InlineKeyboardButton("📋 Pending Orders", callback_data=_cb(CB_DATA, "orders")),
            ],
            [
                InlineKeyboardButton("👥 Co-Admins", callback_data=_cb(CB_ADMIN, "open")),
                InlineKeyboardButton(
                    f"🌐 Public Mode: {bool_badge(config.public_mode)}",
                    callback_data=_cb(CB_PUBLIC, "open"),
                ),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data=_cb(CB_SET, "open")),
                InlineKeyboardButton("📊 Statistics", callback_data=_cb(CB_STATS, "open")),
            ],
            [InlineKeyboardButton("🔄 Refresh", callback_data=_cb(CB_PANEL, "open"))],
        ]
    )


# ── monitoring (spec §10) ──────────────────────────────────────
def monitoring_controls(config: RuntimeConfig) -> InlineKeyboardMarkup:
    if config.paused:
        # Starting the detectors changes nothing while the global pause holds, so
        # the only offer is to lift it.
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("▶️ RESUME BOT", callback_data=_cb(CB_MON, "resume"))],
                [InlineKeyboardButton("🔄 Refresh Status", callback_data=_cb(CB_MON, "status"))],
            ]
        )
    if config.monitoring_enabled:
        action = InlineKeyboardButton(
            "🔴 STOP MONITORING", callback_data=_cb(CB_MON, "stop")
        )
    else:
        action = InlineKeyboardButton(
            "🟢 START MONITORING", callback_data=_cb(CB_MON, "start")
        )
    return InlineKeyboardMarkup(
        [
            [action],
            [InlineKeyboardButton("⏸️ PAUSE EVERYTHING", callback_data=_cb(CB_MON, "pause"))],
            [InlineKeyboardButton("🔄 Refresh Status", callback_data=_cb(CB_MON, "status"))],
            [back_button()],
        ]
    )


# ── threshold (spec §8 / §27) ──────────────────────────────────
def threshold_panel(config: RuntimeConfig) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(THRESHOLD_PRESETS), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    ("✅ " if abs(config.min_whale_value - value) < 1 else "") + fmt_usd(value, 0),
                    callback_data=_cb(CB_THRESH, "set", value),
                )
                for value in THRESHOLD_PRESETS[index : index + 3]
            ]
        )
    rows.append(
        [InlineKeyboardButton("✏️ Custom amount", callback_data=_cb(CB_THRESH, "prompt"))]
    )
    rows.append([back_button()])
    return InlineKeyboardMarkup(rows)


# ── margin gate ────────────────────────────────────────────────
def margin_panel(config: RuntimeConfig) -> InlineKeyboardMarkup:
    """Minimum ``marginUsed`` a position must carry before it alerts.

    Separate from the threshold panel on purpose: margin is collateral at risk,
    the threshold is notional value, and one is not a substitute for the other.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(MARGIN_PRESETS), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    ("✅ " if abs(config.min_margin_value - value) < 1 else "")
                    + ("🚫 Off" if value == 0 else fmt_usd(value, 0)),
                    callback_data=_cb(CB_MARGIN, "set", value),
                )
                for value in MARGIN_PRESETS[index : index + 3]
            ]
        )
    rows.append([InlineKeyboardButton("✏️ Custom margin", callback_data=_cb(CB_MARGIN, "prompt"))])
    rows.append([back_button()])
    return InlineKeyboardMarkup(rows)


def cooldown_panel(config: RuntimeConfig) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                ("✅ " if config.alert_cooldown_seconds == value else "")
                + (f"{value}s" if value else "off"),
                callback_data=_cb(CB_SET, "cooldown", value),
            )
            for value in COOLDOWN_PRESETS
        ],
        [InlineKeyboardButton("✏️ Custom seconds", callback_data=_cb(CB_SET, "cooldown_prompt"))],
        [back_button(CB_SET)],
    ]
    return InlineKeyboardMarkup(rows)


# ── coins (spec §7) ────────────────────────────────────────────
def coin_panel(config: RuntimeConfig, available: Sequence[str] = ()) -> InlineKeyboardMarkup:
    """``ALL COINS`` vs an explicit selection, plus per-coin toggles."""
    mode = "✅ ALL COINS" if config.all_coins else "🔘 ALL COINS"
    selected = "🔘 SELECTED COINS" if config.all_coins else "✅ SELECTED COINS"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(mode, callback_data=_cb(CB_COIN, "all", 1)),
            InlineKeyboardButton(selected, callback_data=_cb(CB_COIN, "all", 0)),
        ]
    ]

    # Offer the live Hyperliquid universe when we have it, suggestions otherwise.
    universe = [c.upper() for c in available] or list(SUGGESTED_COINS)
    listed = list(dict.fromkeys(list(config.coins) + universe))[:24]
    for index in range(0, len(listed), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    ("✅ " if coin in config.coins else "➕ ") + coin,
                    callback_data=_cb(CB_COIN, "toggle", coin),
                )
                for coin in listed[index : index + 3]
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("✏️ Set list", callback_data=_cb(CB_COIN, "prompt")),
            InlineKeyboardButton("🧹 Clear", callback_data=_cb(CB_COIN, "clear")),
        ]
    )
    rows.append([back_button()])
    return InlineKeyboardMarkup(rows)


# ── admin management (spec §15) ────────────────────────────────
def admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add Co-Admin", callback_data=_cb(CB_ADMIN, "add"))],
            [InlineKeyboardButton("➖ Remove Co-Admin", callback_data=_cb(CB_ADMIN, "remove"))],
            [InlineKeyboardButton("📋 List Co-Admins", callback_data=_cb(CB_ADMIN, "list"))],
            [back_button()],
        ]
    )


def admin_roster_panel() -> InlineKeyboardMarkup:
    """Keyboard for the 📋 List Co-Admins panel — distinct from the admin home."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data=_cb(CB_ADMIN, "list"))],
            [back_button(CB_ADMIN)],
        ]
    )


def admin_remove_panel(co_admins: Sequence[dict[str, object]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"➖ {entry.get('username') or entry['telegram_id']}",
                callback_data=_cb(CB_ADMIN, "remove_id", entry["telegram_id"]),
            )
        ]
        for entry in co_admins
    ]
    rows.append([back_button(CB_ADMIN)])
    return InlineKeyboardMarkup(rows)


# ── public mode (spec §11) ─────────────────────────────────────
def public_mode_panel(config: RuntimeConfig) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ("✅ " if config.public_mode else "") + "🌐 PUBLIC",
                    callback_data=_cb(CB_PUBLIC, "set", 1),
                ),
                InlineKeyboardButton(
                    ("✅ " if not config.public_mode else "") + "🔒 PRIVATE",
                    callback_data=_cb(CB_PUBLIC, "set", 0),
                ),
            ],
            [back_button()],
        ]
    )


# ── settings (spec §27) ────────────────────────────────────────
def settings_panel(config: RuntimeConfig) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💰 Minimum Whale Value", callback_data=_cb(CB_THRESH, "open")
                )
            ],
            [
                InlineKeyboardButton(
                    "🏦 Minimum Margin: "
                    + (fmt_usd(config.min_margin_value, 0) if config.margin_gate_enabled else "off"),
                    callback_data=_cb(CB_MARGIN, "open"),
                )
            ],
            [InlineKeyboardButton("🪙 Monitored Coins", callback_data=_cb(CB_COIN, "open"))],            [
                InlineKeyboardButton(
                    f"📡 Monitoring: {bool_badge(config.monitoring_enabled)}",
                    callback_data=_cb(CB_MON, "status"),
                )
            ],
            [
                InlineKeyboardButton(
                    f"🌐 Public Mode: {bool_badge(config.public_mode)}",
                    callback_data=_cb(CB_PUBLIC, "open"),
                )
            ],
            [InlineKeyboardButton("🔔 Alert Settings", callback_data=_cb(CB_SET, "alerts"))],
            [
                InlineKeyboardButton(
                    f"⏱ Cooldown: {config.alert_cooldown_seconds}s",
                    callback_data=_cb(CB_SET, "cooldown_open"),
                )
            ],
            [InlineKeyboardButton("👥 Admins", callback_data=_cb(CB_ADMIN, "open"))],
            [InlineKeyboardButton("📊 Statistics", callback_data=_cb(CB_STATS, "open"))],
            [back_button()],
        ]
    )


def alert_settings_panel(config: RuntimeConfig) -> InlineKeyboardMarkup:
    """Per-detector switches. Each maps to a persisted boolean setting."""
    toggles = (
        ("Large trades", "enable_trade_detector", config.enable_trade_detector),
        ("Position changes", "enable_position_detector", config.enable_position_detector),
        ("Resting orders", "enable_order_detector", config.enable_order_detector),
        ("Order cancellations", "enable_order_cancel_alerts", config.enable_order_cancel_alerts),
        ("Wallet tracking", "enable_wallet_tracking", config.enable_wallet_tracking),
        ("Order book levels", "enable_book_scanner", config.enable_book_scanner),
    )
    rows = [
        [
            InlineKeyboardButton(
                f"{'🟢' if value else '🔴'} {label}",
                callback_data=_cb(CB_SET, "toggle", key),
            )
        ]
        for label, key, value in toggles
    ]
    rows.append([back_button(CB_SET)])
    return InlineKeyboardMarkup(rows)


# ── public user interface (spec §12) ───────────────────────────
def public_menu() -> InlineKeyboardMarkup:
    """No administration controls — ordinary users cannot reach them."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📡 Live Signals", callback_data=_cb(CB_DATA, "live"))],
            [InlineKeyboardButton("🐋 Recent Whales", callback_data=_cb(CB_DATA, "whales"))],
            [InlineKeyboardButton("📋 Large Orders", callback_data=_cb(CB_DATA, "orders"))],
            [InlineKeyboardButton("🪙 Coins", callback_data=_cb(CB_DATA, "coins"))],
            [InlineKeyboardButton("ℹ️ About", callback_data=_cb(CB_DATA, "about"))],
        ]
    )


def data_panel(view: str, *, admin: bool) -> InlineKeyboardMarkup:
    """Refresh / navigation footer for read-only list views."""
    rows = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=_cb(CB_DATA, view)),
            InlineKeyboardButton(
                "🐋 Whales", callback_data=_cb(CB_DATA, "whales")
            ),
            InlineKeyboardButton("📋 Orders", callback_data=_cb(CB_DATA, "orders")),
        ],
        [
            InlineKeyboardButton("📈 Positions", callback_data=_cb(CB_DATA, "positions")),
            InlineKeyboardButton("🪙 Coins", callback_data=_cb(CB_DATA, "coins")),
        ],
    ]
    rows.append([back_button(CB_PANEL if admin else CB_DATA)])
    return InlineKeyboardMarkup(rows)


def stats_panel(admin: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data=_cb(CB_STATS, "open"))],
            [InlineKeyboardButton("🩺 Diagnostics", callback_data=_cb(CB_STATS, "diag"))],
            [back_button(CB_PANEL if admin else CB_DATA)],
        ]
    )


def cancel_prompt() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Cancel", callback_data=_cb(CB_PANEL, "cancel"))]]
    )
