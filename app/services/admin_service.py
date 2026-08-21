"""Role-based access control.

Three roles, exactly as the permission matrix specifies:

============================  ==========  ========  =========
Capability                    MAIN_ADMIN  CO_ADMIN  USER
============================  ==========  ========  =========
View signals / whale events   yes         yes       if public
Start / stop monitoring       yes         yes       no
Change threshold / coins      yes         yes       no
Change public mode            yes         yes       no
Add / remove co-admin         yes         **no**    no
View admin list               yes         yes       no
Change or remove main admin   **no**      **no**    no
============================  ==========  ========  =========

Two rules are enforced in code rather than left to convention:

* The main admin is defined by ``MAIN_ADMIN_ID`` in the environment and cannot
  be removed, demoted or transferred through any command or callback — there is
  no code path that writes ``MAIN_ADMIN`` to another user or deletes that row.
* Every check takes the Telegram user id from the *update object*, never from
  callback data or command arguments, so a crafted callback cannot escalate.

The role table is cached in memory and refreshed on every mutation, so the hot
path (a callback query) does not hit the database.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from app.config import Settings
from app.database.base import Database
from app.database.repository import AdminRepository, AuditRepository, UserRepository
from app.utils.logging import get_logger

log = get_logger(__name__)

ROLE_MAIN = "MAIN_ADMIN"
ROLE_CO = "CO_ADMIN"
ROLE_USER = "USER"


class Capability(str, Enum):
    VIEW_PUBLIC = "view_public"
    VIEW_WHALES = "view_whales"
    CONTROL_MONITORING = "control_monitoring"
    CHANGE_THRESHOLD = "change_threshold"
    CHANGE_COINS = "change_coins"
    CHANGE_PUBLIC_MODE = "change_public_mode"
    CHANGE_SETTINGS = "change_settings"
    VIEW_ADMINS = "view_admins"
    MANAGE_ADMINS = "manage_admins"
    VIEW_STATS = "view_stats"
    VIEW_AUDIT = "view_audit"
    MANAGE_WALLETS = "manage_wallets"


#: Capabilities that a co-admin shares with the main admin.
CO_ADMIN_CAPABILITIES = frozenset(
    {
        Capability.VIEW_PUBLIC,
        Capability.VIEW_WHALES,
        Capability.CONTROL_MONITORING,
        Capability.CHANGE_THRESHOLD,
        Capability.CHANGE_COINS,
        Capability.CHANGE_PUBLIC_MODE,
        Capability.CHANGE_SETTINGS,
        Capability.VIEW_ADMINS,
        Capability.VIEW_STATS,
        Capability.MANAGE_WALLETS,
    }
)

#: Main-admin-only capabilities. MANAGE_ADMINS is deliberately absent above:
#: a co-admin can neither add nor remove another co-admin.
MAIN_ONLY_CAPABILITIES = frozenset(
    {Capability.MANAGE_ADMINS, Capability.VIEW_AUDIT}
)

#: What an ordinary user may do, and only while public mode is on.
PUBLIC_CAPABILITIES = frozenset({Capability.VIEW_PUBLIC, Capability.VIEW_WHALES})


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated caller behind an update."""

    telegram_id: int
    role: str
    username: str | None = None

    @property
    def is_main_admin(self) -> bool:
        return self.role == ROLE_MAIN

    @property
    def is_admin(self) -> bool:
        return self.role in (ROLE_MAIN, ROLE_CO)

    @property
    def role_label(self) -> str:
        return {ROLE_MAIN: "Main Admin", ROLE_CO: "Co-Admin"}.get(self.role, "User")


class AdminError(Exception):
    """A refused administrative action, with a user-safe message."""


class AdminService:
    def __init__(self, database: Database, env: Settings) -> None:
        self.db = database
        self.env = env
        self.main_admin_id = int(env.main_admin_id or 0)
        self._roles: dict[int, str] = {}
        self._lock = asyncio.Lock()

    # ── bootstrap ─────────────────────────────────────────────
    async def load(self) -> dict[int, str]:
        """Install the env-configured main admin and cache all roles."""
        async with self.db.session() as session:
            if self.main_admin_id:
                await AdminRepository.ensure_main(session, self.main_admin_id)
            roles = await AdminRepository.roles(session)
        # The env value always wins, even if the cached table disagrees.
        if self.main_admin_id:
            roles[self.main_admin_id] = ROLE_MAIN
        self._roles = roles
        log.info(
            "Admin roles loaded",
            extra={
                "main_admin": bool(self.main_admin_id),
                "co_admins": sum(1 for r in roles.values() if r == ROLE_CO),
            },
        )
        return dict(self._roles)

    async def refresh(self) -> None:
        async with self.db.session() as session:
            roles = await AdminRepository.roles(session)
        if self.main_admin_id:
            roles[self.main_admin_id] = ROLE_MAIN
        self._roles = roles

    # ── authorization ─────────────────────────────────────────
    def role_of(self, telegram_id: int | None) -> str:
        if telegram_id is None:
            return ROLE_USER
        telegram_id = int(telegram_id)
        if telegram_id == self.main_admin_id and self.main_admin_id:
            return ROLE_MAIN
        return self._roles.get(telegram_id, ROLE_USER)

    def actor(self, telegram_id: int | None, username: str | None = None) -> Actor:
        return Actor(int(telegram_id or 0), self.role_of(telegram_id), username)

    def is_admin(self, telegram_id: int | None) -> bool:
        return self.role_of(telegram_id) in (ROLE_MAIN, ROLE_CO)

    def is_main_admin(self, telegram_id: int | None) -> bool:
        return self.role_of(telegram_id) == ROLE_MAIN

    def can(
        self, telegram_id: int | None, capability: Capability, *, public_mode: bool = False
    ) -> bool:
        """The single authority for "may this Telegram id do this?"."""
        role = self.role_of(telegram_id)
        if role == ROLE_MAIN:
            return True
        if role == ROLE_CO:
            return capability in CO_ADMIN_CAPABILITIES
        return public_mode and capability in PUBLIC_CAPABILITIES

    def require(
        self, telegram_id: int | None, capability: Capability, *, public_mode: bool = False
    ) -> Actor:
        if not self.can(telegram_id, capability, public_mode=public_mode):
            role = self.role_of(telegram_id)
            log.warning(
                "Unauthorized action refused",
                extra={"telegram_id": telegram_id, "role": role, "capability": capability.value},
            )
            raise AdminError(self.denial_message(capability, role, public_mode))
        return self.actor(telegram_id)

    @staticmethod
    def denial_message(capability: Capability, role: str, public_mode: bool) -> str:
        if capability is Capability.MANAGE_ADMINS and role == ROLE_CO:
            return (
                "🚫 Only the Main Admin can manage administrators.\n"
                "Co-Admins cannot add or remove other admins."
            )
        if role == ROLE_USER and not public_mode:
            return (
                "🔒 This bot is currently private.\n"
                "Whale monitoring is available only to authorized administrators."
            )
        return "🚫 You are not authorized to use this control."

    # ── admin management (main admin only) ────────────────────
    async def add_co_admin(
        self,
        actor_id: int,
        target_id: int,
        username: str | None = None,
        note: str | None = None,
    ) -> Actor:
        self.require(actor_id, Capability.MANAGE_ADMINS)
        target_id = int(target_id)
        if target_id <= 0:
            raise AdminError("❌ Invalid Telegram user ID.")
        if target_id == self.main_admin_id:
            raise AdminError("ℹ️ That user is already the Main Admin.")
        if self.role_of(target_id) == ROLE_CO:
            raise AdminError("ℹ️ That user is already a Co-Admin.")

        async with self._lock:
            async with self.db.session() as session:
                _row, created = await AdminRepository.add_co_admin(
                    session, target_id, added_by=actor_id, username=username, note=note
                )
                if created:
                    await AuditRepository.record(
                        session, actor_id, "add:co_admin", str(target_id), None, ROLE_CO
                    )
            await self.refresh()
        if not created:
            raise AdminError("ℹ️ That user is already an administrator.")
        log.info("Co-admin added", extra={"actor": actor_id, "target": target_id})
        return self.actor(target_id, username)

    async def remove_co_admin(self, actor_id: int, target_id: int) -> None:
        self.require(actor_id, Capability.MANAGE_ADMINS)
        target_id = int(target_id)
        # The main admin is defined by MAIN_ADMIN_ID and is not removable —
        # not by a co-admin, and not by the main admin either.
        if target_id == self.main_admin_id or self.role_of(target_id) == ROLE_MAIN:
            raise AdminError(
                "🚫 The Main Admin cannot be removed.\n"
                "Change MAIN_ADMIN_ID in the deployment environment instead."
            )
        if self.role_of(target_id) != ROLE_CO:
            raise AdminError("ℹ️ That user is not a Co-Admin.")

        async with self._lock:
            async with self.db.session() as session:
                removed = await AdminRepository.remove(session, target_id)
                if removed:
                    await AuditRepository.record(
                        session, actor_id, "remove:co_admin", str(target_id), ROLE_CO, None
                    )
            await self.refresh()
        if not removed:
            raise AdminError("❌ Could not remove that administrator.")
        log.info("Co-admin removed", extra={"actor": actor_id, "target": target_id})

    async def list_admins(self, actor_id: int) -> list[dict[str, object]]:
        self.require(actor_id, Capability.VIEW_ADMINS)
        async with self.db.session() as session:
            rows = await AdminRepository.list(session)
        out: list[dict[str, object]] = []
        seen = set()
        for row in rows:
            role = ROLE_MAIN if row.telegram_id == self.main_admin_id else row.role
            seen.add(int(row.telegram_id))
            out.append(
                {
                    "telegram_id": int(row.telegram_id),
                    "role": role,
                    "username": row.username,
                    "added_by": row.added_by,
                    "created_at": row.created_at,
                }
            )
        if self.main_admin_id and self.main_admin_id not in seen:
            out.insert(
                0,
                {
                    "telegram_id": self.main_admin_id,
                    "role": ROLE_MAIN,
                    "username": None,
                    "added_by": None,
                    "created_at": None,
                },
            )
        out.sort(key=lambda item: (item["role"] != ROLE_MAIN, item["telegram_id"]))
        return out

    @property
    def co_admin_count(self) -> int:
        return sum(1 for role in self._roles.values() if role == ROLE_CO)

    @property
    def admin_ids(self) -> list[int]:
        ids = {tid for tid, role in self._roles.items() if role in (ROLE_MAIN, ROLE_CO)}
        if self.main_admin_id:
            ids.add(self.main_admin_id)
        return sorted(ids)

    # ── user registry ─────────────────────────────────────────
    async def register_user(
        self,
        telegram_id: int,
        chat_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        """Record contact details so alerts can be delivered later."""
        try:
            async with self.db.session() as session:
                await UserRepository.upsert(
                    session,
                    telegram_id=telegram_id,
                    chat_id=chat_id,
                    username=username,
                    first_name=first_name,
                )
        except Exception:
            log.exception("Could not register user", extra={"telegram_id": telegram_id})

    def stats(self) -> dict[str, object]:
        return {
            "main_admin_configured": bool(self.main_admin_id),
            "co_admins": self.co_admin_count,
            "total_admins": len(self.admin_ids),
        }
