"""Data access layer.

Plain ``select`` / ``update`` constructs with bound parameters throughout — no
string interpolation of user input anywhere, which is what keeps SQL injection
off the table. Upserts are read-then-write rather than dialect-specific
``ON CONFLICT`` so the same code runs on Postgres and on SQLite (tests).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Admin,
    AdminAudit,
    AlertHistory,
    BotLog,
    OrderRecord,
    PositionRecord,
    Setting,
    TrackedCoin,
    TrackedWallet,
    User,
    Wallet,
    WhaleEvent,
)
from app.utils.formatting import utc_now

ROLE_MAIN = "MAIN_ADMIN"
ROLE_CO = "CO_ADMIN"


# ── settings ───────────────────────────────────────────────────

class SettingsRepository:
    @staticmethod
    async def all(session: AsyncSession) -> dict[str, str]:
        rows = (await session.execute(select(Setting))).scalars().all()
        return {row.key: row.value for row in rows}

    @staticmethod
    async def get(session: AsyncSession, key: str) -> str | None:
        return await session.scalar(select(Setting.value).where(Setting.key == key))

    @staticmethod
    async def set(
        session: AsyncSession, key: str, value: str, updated_by: int | None = None
    ) -> None:
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value, updated_by=updated_by))
        else:
            row.value = value
            row.updated_by = updated_by

    @staticmethod
    async def delete(session: AsyncSession, key: str) -> None:
        await session.execute(delete(Setting).where(Setting.key == key))


# ── admins ─────────────────────────────────────────────────────

class AdminRepository:
    @staticmethod
    async def list(session: AsyncSession) -> list[Admin]:
        stmt = select(Admin).order_by(Admin.role.desc(), Admin.created_at)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def get(session: AsyncSession, telegram_id: int) -> Admin | None:
        return await session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))

    @staticmethod
    async def ensure_main(
        session: AsyncSession, telegram_id: int, username: str | None = None
    ) -> Admin:
        """Idempotently install the env-configured main admin as the sole owner."""
        row = await AdminRepository.get(session, telegram_id)
        if row is None:
            row = Admin(telegram_id=telegram_id, role=ROLE_MAIN, username=username)
            session.add(row)
        else:
            row.role = ROLE_MAIN
            if username:
                row.username = username
        # Any stale MAIN_ADMIN row (e.g. MAIN_ADMIN_ID was changed in Railway)
        # is demoted rather than deleted, so the audit trail survives.
        await session.execute(
            update(Admin)
            .where(Admin.role == ROLE_MAIN, Admin.telegram_id != telegram_id)
            .values(role=ROLE_CO)
        )
        return row

    @staticmethod
    async def add_co_admin(
        session: AsyncSession,
        telegram_id: int,
        added_by: int,
        username: str | None = None,
        note: str | None = None,
    ) -> tuple[Admin, bool]:
        """Returns ``(row, created)``. Never downgrades an existing main admin."""
        row = await AdminRepository.get(session, telegram_id)
        if row is not None:
            return row, False
        row = Admin(
            telegram_id=telegram_id,
            role=ROLE_CO,
            added_by=added_by,
            username=username,
            note=note,
        )
        session.add(row)
        return row, True

    @staticmethod
    async def remove(session: AsyncSession, telegram_id: int) -> bool:
        row = await AdminRepository.get(session, telegram_id)
        if row is None or row.role == ROLE_MAIN:
            return False
        await session.delete(row)
        return True

    @staticmethod
    async def roles(session: AsyncSession) -> dict[int, str]:
        rows = (await session.execute(select(Admin.telegram_id, Admin.role))).all()
        return {int(tid): role for tid, role in rows}


# ── users ──────────────────────────────────────────────────────

class UserRepository:
    @staticmethod
    async def upsert(
        session: AsyncSession,
        telegram_id: int,
        chat_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> User:
        row = await session.get(User, telegram_id)
        now = utc_now()
        if row is None:
            row = User(
                telegram_id=telegram_id,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                last_seen_at=now,
            )
            session.add(row)
        else:
            row.chat_id = chat_id
            row.last_seen_at = now
            if username:
                row.username = username
            if first_name:
                row.first_name = first_name
            row.is_blocked = False
        return row

    @staticmethod
    async def get(session: AsyncSession, telegram_id: int) -> User | None:
        return await session.get(User, telegram_id)

    @staticmethod
    async def set_subscribed(session: AsyncSession, telegram_id: int, value: bool) -> None:
        await session.execute(
            update(User).where(User.telegram_id == telegram_id).values(is_subscribed=value)
        )

    @staticmethod
    async def mark_blocked(session: AsyncSession, telegram_id: int) -> None:
        await session.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(is_blocked=True, is_subscribed=False)
        )

    @staticmethod
    async def subscribers(session: AsyncSession) -> list[User]:
        stmt = select(User).where(User.is_subscribed.is_(True), User.is_blocked.is_(False))
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def unsubscribed_ids(session: AsyncSession) -> set[int]:
        """Users who sent /stop (or blocked the bot).

        Only *explicit* opt-outs are returned. A user with no row at all is not
        in this set, so a never-seen administrator still receives alerts.
        """
        stmt = select(User.telegram_id).where(
            or_(User.is_subscribed.is_(False), User.is_blocked.is_(True))
        )
        return {int(telegram_id) for telegram_id in (await session.execute(stmt)).scalars().all()}

    @staticmethod
    async def bump_alert_counts(session: AsyncSession, telegram_ids: Sequence[int]) -> None:
        if not telegram_ids:
            return
        await session.execute(
            update(User)
            .where(User.telegram_id.in_(list(telegram_ids)))
            .values(alerts_received=User.alerts_received + 1)
        )

    @staticmethod
    async def count(session: AsyncSession) -> int:
        return int(await session.scalar(select(func.count()).select_from(User)) or 0)


# ── coins ──────────────────────────────────────────────────────

class CoinRepository:
    @staticmethod
    async def enabled(session: AsyncSession) -> list[str]:
        stmt = select(TrackedCoin.coin).where(TrackedCoin.enabled.is_(True)).order_by(TrackedCoin.coin)
        return [c for c in (await session.execute(stmt)).scalars().all()]

    @staticmethod
    async def all(session: AsyncSession) -> list[TrackedCoin]:
        stmt = select(TrackedCoin).order_by(TrackedCoin.coin)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def add(session: AsyncSession, coin: str, admin_id: int | None = None) -> bool:
        coin = coin.upper()
        row = await session.get(TrackedCoin, coin)
        if row is None:
            session.add(TrackedCoin(coin=coin, enabled=True, added_by=admin_id))
            return True
        if not row.enabled:
            row.enabled = True
            return True
        return False

    @staticmethod
    async def remove(session: AsyncSession, coin: str) -> bool:
        row = await session.get(TrackedCoin, coin.upper())
        if row is None:
            return False
        await session.delete(row)
        return True

    @staticmethod
    async def replace(
        session: AsyncSession, coins: Sequence[str], admin_id: int | None = None
    ) -> tuple[list[str], list[str]]:
        """Make the enabled set exactly ``coins``, touching only what differs.

        Returns ``(added, removed)``. This is deliberately a diff and not a
        ``DELETE FROM tracked_coins`` followed by re-inserts: a full rewrite
        discards ``added_by`` and ``created_at`` for coins that were not changing,
        and — worse — it makes two admins working at the same time destructive to
        each other, because the second write would resurrect rows the first one
        removed. Only the caller that explicitly means "make the list exactly
        this" should call it at all; ``add``/``remove`` are the patch operations.
        """
        wanted = list(dict.fromkeys(c.upper() for c in coins if c.strip()))
        existing = {row.coin: row for row in await CoinRepository.all(session)}

        added: list[str] = []
        for coin in wanted:
            row = existing.get(coin)
            if row is None:
                session.add(TrackedCoin(coin=coin, enabled=True, added_by=admin_id))
                added.append(coin)
            elif not row.enabled:
                row.enabled = True
                added.append(coin)

        removed: list[str] = []
        for coin, row in existing.items():
            if coin not in wanted:
                await session.delete(row)
                removed.append(coin)

        return added, sorted(removed)


# ── wallets ────────────────────────────────────────────────────

class WalletRepository:
    @staticmethod
    async def record_activity(
        session: AsyncSession,
        address: str,
        *,
        coin: str | None = None,
        side: str | None = None,
        notional: float = 0.0,
        position_value: float | None = None,
        order_value: float | None = None,
        account_value: float | None = None,
        seen_at: datetime | None = None,
    ) -> Wallet:
        address = address.lower()
        row = await session.get(Wallet, address)
        now = seen_at or utc_now()
        if row is None:
            row = Wallet(address=address, first_seen=now, last_seen=now, coins={})
            session.add(row)
            await session.flush()
        row.last_seen = now
        row.event_count += 1
        row.total_notional += abs(notional or 0.0)
        if side == "LONG" or side == "BUY":
            row.long_volume += abs(notional or 0.0)
        elif side == "SHORT" or side == "SELL":
            row.short_volume += abs(notional or 0.0)
        if position_value is not None:
            row.largest_position = max(row.largest_position, abs(position_value))
        if order_value is not None:
            row.largest_order = max(row.largest_order, abs(order_value))
        if account_value is not None:
            row.account_value = account_value
        if coin:
            counts = dict(row.coins or {})
            counts[coin] = int(counts.get(coin, 0)) + 1
            row.coins = counts  # reassign so SQLAlchemy detects the change
        return row

    @staticmethod
    async def get(session: AsyncSession, address: str) -> Wallet | None:
        return await session.get(Wallet, address.lower())

    @staticmethod
    async def top(session: AsyncSession, limit: int = 10) -> list[Wallet]:
        stmt = select(Wallet).order_by(Wallet.total_notional.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def count(session: AsyncSession) -> int:
        return int(await session.scalar(select(func.count()).select_from(Wallet)) or 0)

    # ── admin-pinned watchlist ─────────────────────────────────
    @staticmethod
    async def add_tracked(
        session: AsyncSession, address: str, added_by: int | None, label: str | None = None
    ) -> bool:
        address = address.lower()
        row = await session.get(TrackedWallet, address)
        if row is not None:
            if label:
                row.label = label
            return False
        session.add(TrackedWallet(address=address, added_by=added_by, label=label))
        return True

    @staticmethod
    async def remove_tracked(session: AsyncSession, address: str) -> bool:
        row = await session.get(TrackedWallet, address.lower())
        if row is None:
            return False
        await session.delete(row)
        return True

    @staticmethod
    async def tracked(session: AsyncSession) -> list[TrackedWallet]:
        stmt = select(TrackedWallet).order_by(TrackedWallet.created_at)
        return list((await session.execute(stmt)).scalars().all())


# ── whale events ───────────────────────────────────────────────

class EventRepository:
    @staticmethod
    async def insert(session: AsyncSession, **fields: Any) -> WhaleEvent:
        row = WhaleEvent(**fields)
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    async def mark_alerted(session: AsyncSession, event_id: int) -> None:
        await session.execute(
            update(WhaleEvent).where(WhaleEvent.id == event_id).values(alerted=True)
        )

    @staticmethod
    async def seen_recently(session: AsyncSession, dedup_key: str, since: datetime) -> bool:
        stmt = (
            select(WhaleEvent.id)
            .where(WhaleEvent.dedup_key == dedup_key, WhaleEvent.created_at >= since)
            .limit(1)
        )
        return (await session.scalar(stmt)) is not None

    @staticmethod
    async def recent(
        session: AsyncSession,
        limit: int = 10,
        since: datetime | None = None,
        coin: str | None = None,
        event_types: Sequence[str] | None = None,
        wallet: str | None = None,
        min_notional: float | None = None,
    ) -> list[WhaleEvent]:
        stmt = select(WhaleEvent).order_by(WhaleEvent.event_time.desc()).limit(limit)
        if since is not None:
            stmt = stmt.where(WhaleEvent.event_time >= since)
        if coin:
            stmt = stmt.where(WhaleEvent.coin == coin.upper())
        if event_types:
            stmt = stmt.where(WhaleEvent.event_type.in_(list(event_types)))
        if wallet:
            stmt = stmt.where(WhaleEvent.wallet == wallet.lower())
        if min_notional is not None:
            stmt = stmt.where(WhaleEvent.notional >= min_notional)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def summary(session: AsyncSession, since: datetime | None = None) -> dict[str, Any]:
        def scope(stmt: Any) -> Any:
            return stmt.where(WhaleEvent.event_time >= since) if since is not None else stmt

        total = int(await session.scalar(scope(select(func.count()).select_from(WhaleEvent))) or 0)
        notional = float(await session.scalar(scope(select(func.sum(WhaleEvent.notional)))) or 0.0)
        largest = float(await session.scalar(scope(select(func.max(WhaleEvent.notional)))) or 0.0)
        longs = int(
            await session.scalar(
                scope(select(func.count()).select_from(WhaleEvent)).where(
                    WhaleEvent.side.in_(["LONG", "BUY"])
                )
            )
            or 0
        )
        shorts = int(
            await session.scalar(
                scope(select(func.count()).select_from(WhaleEvent)).where(
                    WhaleEvent.side.in_(["SHORT", "SELL"])
                )
            )
            or 0
        )
        by_coin = (
            await session.execute(
                scope(
                    select(WhaleEvent.coin, func.count().label("n"), func.sum(WhaleEvent.notional))
                )
                .group_by(WhaleEvent.coin)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        by_type = (
            await session.execute(
                scope(select(WhaleEvent.event_type, func.count()))
                .group_by(WhaleEvent.event_type)
                .order_by(func.count().desc())
            )
        ).all()
        return {
            "total": total,
            "longs": longs,
            "shorts": shorts,
            "notional": notional,
            "largest": largest,
            "by_coin": [
                {"coin": c, "count": int(n), "notional": float(v or 0)} for c, n, v in by_coin
            ],
            "by_type": [{"type": t, "count": int(n)} for t, n in by_type],
        }

    @staticmethod
    async def prune(session: AsyncSession, older_than: datetime) -> int:
        result = await session.execute(delete(WhaleEvent).where(WhaleEvent.created_at < older_than))
        return int(result.rowcount or 0)


# ── orders ─────────────────────────────────────────────────────

class OrderRepository:
    @staticmethod
    async def get(session: AsyncSession, wallet: str, oid: int) -> OrderRecord | None:
        stmt = select(OrderRecord).where(
            OrderRecord.wallet == wallet.lower(), OrderRecord.oid == oid
        )
        return await session.scalar(stmt)

    @staticmethod
    async def upsert(
        session: AsyncSession, wallet: str, oid: int, **fields: Any
    ) -> tuple[OrderRecord, str | None]:
        """Returns ``(row, previous_status)``; ``None`` means the row is new."""
        row = await OrderRepository.get(session, wallet, oid)
        previous = row.status if row is not None else None
        if row is None:
            row = OrderRecord(wallet=wallet.lower(), oid=oid, **fields)
            session.add(row)
            await session.flush()
        else:
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
        return row, previous

    @staticmethod
    async def open_orders(
        session: AsyncSession,
        limit: int = 10,
        coin: str | None = None,
        min_notional: float | None = None,
    ) -> list[OrderRecord]:
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.status == "open")
            .order_by(OrderRecord.notional.desc())
            .limit(limit)
        )
        if coin:
            stmt = stmt.where(OrderRecord.coin == coin.upper())
        if min_notional is not None:
            stmt = stmt.where(OrderRecord.notional >= min_notional)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def close(
        session: AsyncSession, wallet: str, oid: int, status: str, when: datetime | None = None
    ) -> None:
        await session.execute(
            update(OrderRecord)
            .where(OrderRecord.wallet == wallet.lower(), OrderRecord.oid == oid)
            .values(status=status, closed_at=when or utc_now())
        )


# ── positions ──────────────────────────────────────────────────

class PositionRepository:
    @staticmethod
    async def get(session: AsyncSession, wallet: str, coin: str) -> PositionRecord | None:
        stmt = select(PositionRecord).where(
            PositionRecord.wallet == wallet.lower(), PositionRecord.coin == coin.upper()
        )
        return await session.scalar(stmt)

    @staticmethod
    async def upsert(session: AsyncSession, wallet: str, coin: str, **fields: Any) -> PositionRecord:
        row = await PositionRepository.get(session, wallet, coin)
        if row is None:
            row = PositionRecord(wallet=wallet.lower(), coin=coin.upper(), **fields)
            session.add(row)
            await session.flush()
            return row
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    @staticmethod
    async def open_positions(
        session: AsyncSession,
        limit: int = 10,
        coin: str | None = None,
        min_notional: float | None = None,
    ) -> list[PositionRecord]:
        stmt = (
            select(PositionRecord)
            .where(PositionRecord.is_open.is_(True))
            .order_by(PositionRecord.position_value.desc())
            .limit(limit)
        )
        if coin:
            stmt = stmt.where(PositionRecord.coin == coin.upper())
        if min_notional is not None:
            stmt = stmt.where(PositionRecord.position_value >= min_notional)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def by_wallet(session: AsyncSession, wallet: str) -> list[PositionRecord]:
        stmt = (
            select(PositionRecord)
            .where(PositionRecord.wallet == wallet.lower(), PositionRecord.is_open.is_(True))
            .order_by(PositionRecord.position_value.desc())
        )
        return list((await session.execute(stmt)).scalars().all())


# ── alerts ─────────────────────────────────────────────────────

class AlertRepository:
    @staticmethod
    async def record(
        session: AsyncSession,
        dedup_key: str,
        *,
        event_id: int | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        thread_key: str | None = None,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        session.add(
            AlertHistory(
                dedup_key=dedup_key,
                event_id=event_id,
                chat_id=chat_id,
                message_id=message_id,
                thread_key=(thread_key or None),
                ok=ok,
                error=(error or None) if error is None else error[:256],
            )
        )

    @staticmethod
    async def thread_roots(
        session: AsyncSession, since: datetime, limit: int = 500
    ) -> list[tuple[int, str, int]]:
        """``(chat_id, thread_key, message_id)`` for recently delivered alerts.

        Oldest first, so a caller keeping the *first* row per ``(chat, thread)``
        anchors each thread to its earliest still-recent message. Used to restore
        reply threading after a restart — without it every redeploy would start a
        fresh, unconnected thread for wallets already being reported.
        """
        stmt = (
            select(AlertHistory.chat_id, AlertHistory.thread_key, AlertHistory.message_id)
            .where(
                AlertHistory.sent_at >= since,
                AlertHistory.ok.is_(True),
                AlertHistory.thread_key.is_not(None),
                AlertHistory.message_id.is_not(None),
                AlertHistory.chat_id.is_not(None),
            )
            .order_by(AlertHistory.sent_at.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(int(chat_id), str(key), int(message_id)) for chat_id, key, message_id in rows]

    @staticmethod
    async def sent_since(session: AsyncSession, dedup_key: str, since: datetime) -> bool:
        stmt = (
            select(AlertHistory.id)
            .where(
                AlertHistory.dedup_key == dedup_key,
                AlertHistory.sent_at >= since,
                AlertHistory.ok.is_(True),
            )
            .limit(1)
        )
        return (await session.scalar(stmt)) is not None

    @staticmethod
    async def count_since(session: AsyncSession, since: datetime) -> int:
        stmt = select(func.count()).select_from(AlertHistory).where(AlertHistory.sent_at >= since)
        return int(await session.scalar(stmt) or 0)

    @staticmethod
    async def recent_keys(session: AsyncSession, since: datetime, limit: int = 2000) -> list[str]:
        """Warm the in-memory dedup cache after a restart."""
        stmt = (
            select(AlertHistory.dedup_key)
            .where(AlertHistory.sent_at >= since, AlertHistory.ok.is_(True))
            .order_by(AlertHistory.sent_at.desc())
            .limit(limit)
        )
        return [k for k in (await session.execute(stmt)).scalars().all()]


# ── audit + logs ───────────────────────────────────────────────

class AuditRepository:
    @staticmethod
    async def record(
        session: AsyncSession,
        admin_id: int,
        action: str,
        target: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
    ) -> None:
        session.add(
            AdminAudit(
                admin_id=admin_id,
                action=action[:48],
                target=None if target is None else str(target)[:128],
                old_value=None if old_value is None else str(old_value)[:512],
                new_value=None if new_value is None else str(new_value)[:512],
            )
        )

    @staticmethod
    async def recent(session: AsyncSession, limit: int = 15) -> list[AdminAudit]:
        stmt = select(AdminAudit).order_by(AdminAudit.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())


class LogRepository:
    @staticmethod
    async def write(
        session: AsyncSession,
        level: str,
        source: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            BotLog(
                level=level[:16],
                source=source[:64],
                message=message[:512],
                context=context or {},
            )
        )

    @staticmethod
    async def prune(session: AsyncSession, keep_days: int = 14) -> int:
        cutoff = utc_now() - timedelta(days=keep_days)
        result = await session.execute(delete(BotLog).where(BotLog.created_at < cutoff))
        return int(result.rowcount or 0)
