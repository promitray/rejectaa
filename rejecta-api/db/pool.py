"""Process-wide asyncpg pool for the running API.

The API connects as the least-privilege role ``rejecta_api`` (DATABASE_URL).
When DATABASE_URL is unset the API still starts, so the existing tools keep
working; features that need the database will report it as unconfigured.
"""

import logging

import asyncpg

from config import settings
from db.url import asyncpg_ssl, normalize_postgres_url

logger = logging.getLogger("rejecta.db")

APPLICATION_NAME = "rejecta-api"
DB_STATUS_OK = "ok"
DB_STATUS_UNCONFIGURED = "unconfigured"
DB_STATUS_ERROR = "error"

_pool: asyncpg.Pool | None = None


class DatabaseUnavailableError(RuntimeError):
    """Raised when code needs the database but no pool is available."""


async def init_pool() -> asyncpg.Pool | None:
    """Create the pool once at startup. Fails fast if the URL is set but unusable."""
    global _pool
    if _pool is not None:
        return _pool
    if not settings.DATABASE_URL.strip():
        logger.warning("DATABASE_URL is not set; database features are disabled")
        return None

    _pool = await asyncpg.create_pool(
        dsn=normalize_postgres_url(settings.DATABASE_URL),
        min_size=settings.DB_POOL_MIN_SIZE,
        max_size=max(settings.DB_POOL_MAX_SIZE, settings.DB_POOL_MIN_SIZE, 1),
        command_timeout=settings.DB_COMMAND_TIMEOUT_SECONDS,
        ssl=asyncpg_ssl(settings.DB_SSL_MODE),
        server_settings={
            "application_name": APPLICATION_NAME,
            "search_path": "app",
        },
    )
    logger.info("Database pool ready (min=%s max=%s)", settings.DB_POOL_MIN_SIZE, settings.DB_POOL_MAX_SIZE)
    return _pool


async def close_pool() -> None:
    """Close the pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the pool or raise if the database is not configured."""
    if _pool is None:
        raise DatabaseUnavailableError("Database is not configured")
    return _pool


async def check_database() -> str:
    """Cheap liveness probe used by /health."""
    if _pool is None:
        return DB_STATUS_UNCONFIGURED
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return DB_STATUS_OK
    except Exception:  # noqa: BLE001 - health must never raise
        logger.warning("Database health check failed", exc_info=True)
        return DB_STATUS_ERROR
