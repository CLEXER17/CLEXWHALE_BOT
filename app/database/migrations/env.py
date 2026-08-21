"""Alembic environment.

The database URL is never stored in alembic.ini — it is read from
``DATABASE_URL`` through :mod:`app.config` so that Railway's injected
credentials are the single source of truth and nothing lands in git.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.database import models  # noqa: F401  — registers the tables
from app.database.base import Base

config = context.config

# When Alembic runs in-process (``app.main`` at startup) the application has
# already installed its own handlers, including the secret-redaction filter.
# ``fileConfig`` would replace them, so it is skipped unless Alembic was invoked
# from the command line on its own.
if config.attributes.get("configure_logging", True) and config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

_settings = get_settings()
# '%' is the config-parser interpolation character; escape it for passwords.
config.set_main_option("sqlalchemy.url", _settings.database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=_settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER in place; batch mode keeps dev parity.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
