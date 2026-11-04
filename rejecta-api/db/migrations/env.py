"""Alembic environment: async (asyncpg), raw-SQL migrations, schema ``app``.

Runs as the schema owner using MIGRATION_DATABASE_URL. The version table
lives in ``app.alembic_version``. A session advisory lock stops two deploys
from migrating at the same time.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from config import settings
from db.migration_helpers import APP_SCHEMA
from db.url import asyncpg_ssl, sqlalchemy_async_url

# Arbitrary constant shared by every Rejecta migration run.
MIGRATION_LOCK_KEY = 727_001

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _migration_url() -> str:
    raw = settings.MIGRATION_DATABASE_URL.strip()
    if not raw:
        raise RuntimeError(
            "MIGRATION_DATABASE_URL is not set. Add the Supabase postgres "
            "connection string to rejecta-api/.env (never commit it)."
        )
    return sqlalchemy_async_url(raw)


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=None,
        version_table_schema=APP_SCHEMA,
        transaction_per_migration=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without connecting."""
    _configure(url=_migration_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(
        _migration_url(),
        poolclass=pool.NullPool,
        connect_args={"ssl": asyncpg_ssl(settings.DB_SSL_MODE)},
    )
    async with engine.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        try:
            # The version table lives in app, so the schema must exist first.
            await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {APP_SCHEMA}"))
            await connection.commit()
            await connection.run_sync(_run_sync)
        finally:
            # A failed migration leaves an aborted transaction; clear it first.
            await connection.rollback()
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
            await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
