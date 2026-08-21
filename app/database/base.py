"""Async SQLAlchemy engine / session management.

The connection string always comes from ``DATABASE_URL`` (Railway injects it
for a Postgres service); ``postgres://`` and ``postgresql://`` are normalised
to the asyncpg driver in :mod:`app.config`. A local SQLite fallback exists for
development and tests only — :func:`app.config.validate_runtime` refuses to let
it be used in production.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.utils.formatting import utc_now
from app.utils.logging import get_logger

log = get_logger(__name__)


class Base(DeclarativeBase):
    pass


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str, pool_size: int = 5, max_overflow: int = 5, echo: bool = False) -> None:
        self.url = url
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.echo = echo
        self.connected = False
        self.last_error: str | None = None
        self.last_ok_at = None

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._engine

    async def connect(self) -> None:
        if self._engine is not None:
            return
        kwargs: dict[str, Any] = {"echo": self.echo, "pool_pre_ping": True}
        if not self.is_sqlite:
            kwargs.update(
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_recycle=1800,
            )
        self._engine = create_async_engine(self.url, **kwargs)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    async def disconnect(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None
        self.connected = False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope. Commits on success, rolls back on error."""
        if self._sessionmaker is None:
            await self.connect()
        assert self._sessionmaker is not None
        session = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def create_all(self) -> None:
        """Schema bootstrap for tests / SQLite dev. Production uses Alembic."""
        from app.database import models  # noqa: F401  (register mappers)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def healthcheck(self) -> bool:
        """``SELECT 1``. Never raises — the caller wants a boolean."""
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
            self.connected = True
            self.last_ok_at = utc_now()
            self.last_error = None
            return True
        except Exception as exc:
            self.connected = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    async def wait_until_ready(self, attempts: int = 10, delay: float = 2.0) -> bool:
        """Railway's Postgres may still be booting when we are; retry a while."""
        for attempt in range(1, attempts + 1):
            if await self.healthcheck():
                return True
            log.warning(
                "Database not ready, retrying",
                extra={"attempt": attempt, "of": attempts, "error": self.last_error},
            )
            await asyncio.sleep(min(delay * attempt, 15.0))
        return False

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "dialect": "sqlite" if self.is_sqlite else "postgresql",
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error": self.last_error,
        }


#: Process-wide handle, assigned during startup by :mod:`app.main`.
db: Database | None = None


def set_database(instance: Database) -> Database:
    global db
    db = instance
    return instance


def get_database() -> Database:
    if db is None:
        raise RuntimeError("Database has not been initialised")
    return db
