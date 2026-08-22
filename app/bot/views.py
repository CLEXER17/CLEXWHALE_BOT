"""View builders.

Each function returns ``(text, keyboard)`` and performs whatever reads it needs.
Commands and inline buttons both go through here, which is what makes "every
setting is reachable through buttons *and* commands" (spec §27) true by
construction rather than by discipline: ``/threshold`` and the 💰 button call the
same builder and therefore cannot drift apart.

Views never authorize. The caller has already passed
:func:`app.bot.middleware.permissions.requires`; what the ``actor`` argument
decides here is only *presentation* — for example whether the back button
returns to the admin panel or the public menu.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from telegram import InlineKeyboardMarkup

from app.bot.keyboards import inline
from app.bot.messages import texts
from app.container import AppContainer
from app.database.repository import (
    AdminRepository,
    AuditRepository,
    CoinRepository,
    EventRepository,
    OrderRepository,
    PositionRepository,
    SettingsRepository,
    UserRepository,
    WalletRepository,
)
from app.services.admin_service import Actor, ROLE_CO
from app.services.settings_service import SettingsService
from app.utils.formatting import utc_now

View = tuple[str, InlineKeyboardMarkup | None]

#: How many rows a list view shows. Telegram's 4096-character message limit is
#: the real constraint; these keep every view comfortably inside it.
LIST_LIMIT = 8
RECENT_WINDOW = timedelta(hours=24)


# ── entry points ───────────────────────────────────────────────
async def start_view(container: AppContainer, actor: Actor) -> View:
    """Spec §11/§12 — admins get the control panel, users the public menu."""
    config = container.settings.config
    if actor.is_admin:
        return texts.start_admin(actor.role_label, config), inline.control_panel(config)
    if config.public_mode:
        return texts.start_public(), inline.public_menu()
    return texts.PRIVATE_NOTICE, None


async def panel_view(container: AppContainer, actor: Actor) -> View:
    config = container.settings.config
    return texts.start_admin(actor.role_label, config), inline.control_panel(config)


async def help_view(container: AppContainer, actor: Actor) -> View:
    text = texts.help_text(admin=actor.is_admin, main_admin=actor.is_main_admin)
    keyboard = inline.control_panel(container.settings.config) if actor.is_admin else None
    if not actor.is_admin and container.settings.config.public_mode:
        keyboard = inline.public_menu()
    return text, keyboard


async def about_view(container: AppContainer, actor: Actor) -> View:
    return texts.about(container.settings.config), inline.data_panel(
        "about", admin=actor.is_admin
    )


# ── monitoring ─────────────────────────────────────────────────
async def status_view(container: AppContainer) -> View:
    config = container.settings.config
    return texts.status_panel(config, container.stats()), inline.monitoring_controls(config)


async def monitoring_view(container: AppContainer) -> View:
    return await status_view(container)


# ── filters ────────────────────────────────────────────────────
async def threshold_view(container: AppContainer) -> View:
    config = container.settings.config
    return texts.threshold_panel(config), inline.threshold_panel(config)


async def margin_view(container: AppContainer) -> View:
    config = container.settings.config
    return texts.margin_panel(config), inline.margin_panel(config)


async def cooldown_view(container: AppContainer) -> View:
    config = container.settings.config
    return texts.settings_panel(config), inline.cooldown_panel(config)


async def coins_view(container: AppContainer, actor: Actor) -> View:
    """Admins get toggles; ordinary users get the read-only list."""
    config = container.settings.config
    monitored = container.engine.monitored_coins
    skipped = container.engine.unmonitored_count
    text = texts.coins_panel(config, monitored, skipped)
    if not actor.is_admin:
        return text, inline.data_panel("coins", admin=False)
    return text, inline.coin_panel(config, container.engine.known_coins)


# ── settings ───────────────────────────────────────────────────
async def settings_view(container: AppContainer) -> View:
    config = container.settings.config
    return texts.settings_panel(config), inline.settings_panel(config)


async def alert_settings_view(container: AppContainer) -> View:
    config = container.settings.config
    return texts.alert_settings(config), inline.alert_settings_panel(config)


async def config_view(container: AppContainer) -> View:
    """The persistence snapshot (spec §27).

    Deliberately reads the ``settings``, ``tracked_coins``, ``admins`` and
    ``tracked_wallets`` tables *directly* rather than ``container.settings.config``.
    Every other view renders the in-memory cache, which is the right thing for a
    control panel — but it cannot answer the question this command exists for:
    "is my configuration actually stored, and will it survive the next redeploy?"
    Reading the rows makes a cache that has drifted from the database visible
    instead of self-confirming.
    """
    settings = container.settings
    async with container.db.session() as session:
        stored = await SettingsRepository.all(session)
        coins = await CoinRepository.enabled(session)
        admins = await AdminRepository.list(session)
        wallets = [row.address for row in await WalletRepository.tracked(session)]
        subscribers = len(await UserRepository.subscribers(session))
    values = {key: SettingsService.decode_stored(raw) for key, raw in stored.items()}
    text = texts.config_snapshot(
        values=values,
        coins=coins,
        admins=[{"telegram_id": row.telegram_id, "role": row.role} for row in admins],
        wallets=wallets,
        subscribers=subscribers,
        cached=settings.config,
        durable=not container.db.is_sqlite,
        bootstrapped_at=settings.bootstrapped_at,
    )
    return text, inline.settings_panel(settings.config)


async def public_mode_view(container: AppContainer) -> View:
    config = container.settings.config
    async with container.db.session() as session:
        subscribers = len(await UserRepository.subscribers(session))
    return texts.public_mode_panel(config, subscribers), inline.public_mode_panel(config)


# ── admin management ───────────────────────────────────────────
async def admin_home_view(container: AppContainer, actor: Actor) -> View:
    entries = await container.admins.list_admins(actor.telegram_id)
    text = texts.admin_list(entries, container.admins.main_admin_id)
    # Only the main admin sees the mutating buttons; a co-admin gets the list.
    keyboard = inline.admin_panel() if actor.is_main_admin else inline.settings_panel(
        container.settings.config
    )
    return text, keyboard


async def admin_roster_view(container: AppContainer, actor: Actor) -> View:
    """The 📋 List Co-Admins panel.

    ``list_admins`` re-checks ``Capability.VIEW_ADMINS`` for ``actor``, so an
    unauthorised caller is refused inside the service, not merely hidden from
    the keyboard.
    """
    entries = await container.admins.list_admins(actor.telegram_id)
    text = texts.admin_roster(entries, container.admins.main_admin_id)
    return text, inline.admin_roster_panel()


async def admin_remove_view(container: AppContainer, actor: Actor) -> View:
    entries = await container.admins.list_admins(actor.telegram_id)
    co_admins = [entry for entry in entries if entry.get("role") == ROLE_CO]
    if not co_admins:
        return "ℹ️ There are no Co-Admins to remove.", inline.admin_panel()
    text = texts.admin_list(entries, container.admins.main_admin_id)
    return text, inline.admin_remove_panel(co_admins)


async def audit_view(container: AppContainer) -> View:
    async with container.db.session() as session:
        rows = await AuditRepository.recent(session, limit=15)
    return texts.audit_list(rows), inline.settings_panel(container.settings.config)


# ── statistics / diagnostics ───────────────────────────────────
async def stats_view(container: AppContainer, actor: Actor) -> View:
    async with container.db.session() as session:
        summary = await EventRepository.summary(session)
    text = texts.statistics_panel(summary, container.stats())
    return text, inline.stats_panel(admin=actor.is_admin)


async def diagnostics_view(container: AppContainer, actor: Actor) -> View:
    health = await container.health()
    text = texts.diagnostics_panel(health, container.stats())
    return text, inline.stats_panel(admin=actor.is_admin)


# ── whale data ─────────────────────────────────────────────────
async def whales_view(container: AppContainer, actor: Actor, coin: str | None = None) -> View:
    config = container.settings.config
    async with container.db.session() as session:
        rows = await EventRepository.recent(session, limit=LIST_LIMIT, coin=coin)
    return texts.whale_list(rows, config), inline.data_panel("whales", admin=actor.is_admin)


async def live_view(container: AppContainer, actor: Actor) -> View:
    config = container.settings.config
    async with container.db.session() as session:
        rows = await EventRepository.recent(
            session, limit=LIST_LIMIT, since=utc_now() - RECENT_WINDOW
        )
    text = texts.live_signals(rows, config, container.engine.connected)
    return text, inline.data_panel("live", admin=actor.is_admin)


async def orders_view(container: AppContainer, actor: Actor, coin: str | None = None) -> View:
    config = container.settings.config
    async with container.db.session() as session:
        rows = await OrderRepository.open_orders(session, limit=LIST_LIMIT, coin=coin)
    return texts.order_list(rows, config), inline.data_panel("orders", admin=actor.is_admin)


async def positions_view(container: AppContainer, actor: Actor, coin: str | None = None) -> View:
    config = container.settings.config
    async with container.db.session() as session:
        rows = await PositionRepository.open_positions(session, limit=LIST_LIMIT, coin=coin)
    return texts.position_list(rows, config), inline.data_panel(
        "positions", admin=actor.is_admin
    )


async def wallets_view(container: AppContainer, actor: Actor) -> View:
    config = container.settings.config
    live = [wallet.as_dict() for wallet in container.engine.tracker.top(limit=6)]
    if not live:
        # The in-memory tracker is empty right after a restart; fall back to the
        # persisted totals so the view is not misleadingly blank.
        async with container.db.session() as session:
            rows = await WalletRepository.top(session, limit=6)
        live = [
            {
                "address": row.address,
                "trades": row.event_count,
                "volume": row.total_notional,
                "coins": sorted((row.coins or {}).keys())[:3],
                # No "position_value" here: the persisted column is a historical
                # maximum, not a live open position, and the two must not be
                # rendered under the same label.
            }
            for row in rows
        ]
    text = texts.wallet_list(list(config.tracked_wallets), live)
    return text, inline.data_panel("wallets", admin=actor.is_admin)


# ── dispatch table for the read-only callbacks ─────────────────
async def data_view(container: AppContainer, actor: Actor, view: str) -> View:
    if view in {"open", "menu"}:
        return await start_view(container, actor)
    if view == "live":
        return await live_view(container, actor)
    if view == "orders":
        return await orders_view(container, actor)
    if view == "positions":
        return await positions_view(container, actor)
    if view == "coins":
        return await coins_view(container, actor)
    if view == "wallets":
        return await wallets_view(container, actor)
    if view == "about":
        return await about_view(container, actor)
    return await whales_view(container, actor)


def summarize(value: Any) -> str:
    """Small helper for toast confirmations."""
    return "" if value is None else str(value)
