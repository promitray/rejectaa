"""End-to-end fixtures against a DISPOSABLE Postgres.

    docker run -d --name rejecta-test-db -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17
    $env:TEST_DATABASE_URL = "postgresql://postgres:postgres@localhost:55432/postgres"
    python -m pytest tests/integration

The fixture migrates the database from scratch (downgrade base, upgrade head),
so it refuses Supabase hosts unless REJECTA_ALLOW_REMOTE_TEST_DB=1.
Tests are skipped when TEST_DATABASE_URL is not set.
"""

import asyncio
import os
import secrets
from collections.abc import Iterator

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from auth import entitlements, limits, partners, tokens, user_cache
from config import settings
from db.migration_helpers import API_ROLE
from db.url import asyncpg_ssl, normalize_postgres_url
from scripts.db_role import api_database_url, scram_sha256_verifier
from scripts.generate_keys import new_key

API_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _test_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("TEST_DATABASE_URL not set (needs a disposable Postgres)")
    if "supabase." in url and os.environ.get("REJECTA_ALLOW_REMOTE_TEST_DB") != "1":
        pytest.exit("Refusing to wipe a Supabase database in tests. Use a local Postgres.", returncode=2)
    return url


async def _wipe(owner_url: str, ssl_mode: str) -> None:
    """Drop everything Rejecta created, whatever state a previous run left."""
    conn = await asyncpg.connect(normalize_postgres_url(owner_url), ssl=asyncpg_ssl(ssl_mode))
    try:
        await conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", API_ROLE):
            await conn.execute(f"DROP ROLE {API_ROLE}")
    finally:
        await conn.close()


async def _set_api_role_password(owner_url: str, ssl_mode: str) -> str:
    password = secrets.token_urlsafe(24)
    conn = await asyncpg.connect(normalize_postgres_url(owner_url), ssl=asyncpg_ssl(ssl_mode))
    try:
        await conn.execute(f"ALTER ROLE {API_ROLE} WITH LOGIN PASSWORD '{scram_sha256_verifier(password)}'")
    finally:
        await conn.close()
    return api_database_url(owner_url, password)


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    owner_url = _test_url()
    ssl_mode = os.environ.get("TEST_DB_SSL_MODE", "disable")
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "MIGRATION_DATABASE_URL", owner_url)
    mp.setattr(settings, "DB_SSL_MODE", ssl_mode)

    cfg = Config(os.path.join(API_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(API_ROOT, "db", "migrations"))
    asyncio.run(_wipe(owner_url, ssl_mode))
    # Exercise every upgrade and downgrade on an empty database, then build
    # the schema the tests use.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    mp.setattr(settings, "DATABASE_URL", asyncio.run(_set_api_role_password(owner_url, ssl_mode)))
    kid, private_b64, _ = new_key()
    mp.setattr(settings, "JWT_PRIVATE_KEY", private_b64)
    mp.setattr(settings, "JWT_KEY_ID", kid)
    mp.setattr(settings, "JWT_PREVIOUS_PUBLIC_KEYS", "")
    mp.setattr(settings, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    mp.setattr(settings, "AUTH_COOKIE_SECURE", False)  # TestClient talks plain http
    mp.setattr(settings, "AUTH_COOKIE_PATH", "/auth")  # no Netlify /api prefix in tests
    mp.setattr(settings, "AUTH_USER_CACHE_SECONDS", 0.0)  # see DB changes immediately
    tokens.reset_keyring()

    from main import app

    with TestClient(app) as test_client:
        yield test_client

    tokens.reset_keyring()
    mp.undo()


@pytest.fixture(autouse=True)
def _fresh_limits(client: TestClient) -> None:
    limits.reset_all()
    user_cache.clear()
    entitlements.invalidate()
    partners.invalidate()
    client.cookies.clear()


@pytest.fixture
def db(client: TestClient):
    """Run ``await fn(conn)`` on the app's event loop with a pooled connection."""
    from db.pool import get_pool

    def run(fn):
        async def call():
            async with get_pool().acquire() as conn:
                return await fn(conn)

        return client.portal.call(call)

    return run
