"""Shared helpers for operational scripts."""

from pathlib import Path

import asyncpg

from config import settings
from db.url import asyncpg_ssl, normalize_postgres_url

API_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = API_ROOT / ".env"


async def connect_owner() -> asyncpg.Connection:
    """Connect as the schema owner (MIGRATION_DATABASE_URL)."""
    if not settings.MIGRATION_DATABASE_URL.strip():
        raise SystemExit("MIGRATION_DATABASE_URL is not set in rejecta-api/.env")
    return await asyncpg.connect(
        dsn=normalize_postgres_url(settings.MIGRATION_DATABASE_URL),
        ssl=asyncpg_ssl(settings.DB_SSL_MODE),
        timeout=20,
    )


async def connect_api(dsn: str | None = None) -> asyncpg.Connection:
    """Connect as the API role (DATABASE_URL unless a DSN is given)."""
    raw = dsn if dsn is not None else settings.DATABASE_URL
    if not raw.strip():
        raise SystemExit("DATABASE_URL is not set. Run: python -m scripts.db_role --write-env")
    return await asyncpg.connect(
        dsn=normalize_postgres_url(raw),
        ssl=asyncpg_ssl(settings.DB_SSL_MODE),
        timeout=20,
        server_settings={"application_name": "rejecta-scripts"},
    )


def update_env_file(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Set ``KEY=value`` in a .env file, keeping every other line untouched."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
