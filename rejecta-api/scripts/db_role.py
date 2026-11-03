"""Set or rotate the password of the API database role ``rejecta_api``.

Run after ``alembic upgrade head`` (migration 0001 creates the role as
NOLOGIN). Generates a 256-bit random password, sends it to Postgres as a
SCRAM-SHA-256 verifier (the plain password never reaches the server or its
logs), enables LOGIN, checks that the new credentials work, and builds the
runtime DATABASE_URL from MIGRATION_DATABASE_URL's host.

    python -m scripts.db_role               # print DATABASE_URL (paste into Railway)
    python -m scripts.db_role --write-env   # write DATABASE_URL into rejecta-api/.env

Re-running rotates the password; the old one stops working immediately.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
from urllib.parse import unquote

from config import settings
from db.migration_helpers import API_ROLE
from db.url import normalize_postgres_url
from scripts._common import ENV_FILE, connect_api, connect_owner, update_env_file

SCRAM_ITERATIONS = 4096
SUPABASE_POOLER_SUFFIX = ".pooler.supabase.com"


def scram_sha256_verifier(password: str) -> str:
    """Return a Postgres SCRAM-SHA-256 verifier (same format as psql \\password)."""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, SCRAM_ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def api_database_url(owner_url: str, password: str) -> str:
    """Same host/port/database as the owner URL, with the API role's credentials.

    Supabase's pooler addresses roles as ``<role>.<project_ref>``; the direct
    host uses the plain role name.
    """
    normalized = normalize_postgres_url(owner_url)
    rest = normalized[len("postgresql://") :]
    credentials, _, location = rest.rpartition("@")
    owner_user = unquote(credentials.partition(":")[0])
    host = location.partition("/")[0].rsplit(":", 1)[0]

    user = API_ROLE
    if host.endswith(SUPABASE_POOLER_SUFFIX) and "." in owner_user:
        user = f"{API_ROLE}.{owner_user.split('.', 1)[1]}"
    return f"postgresql://{user}:{password}@{location}"


async def main(write_env: bool) -> None:
    password = secrets.token_urlsafe(32)
    verifier = scram_sha256_verifier(password)
    # The verifier contains only base64 characters, '$' and ':', never a quote.
    if "'" in verifier:
        raise SystemExit("Refusing to send a malformed verifier")

    owner = await connect_owner()
    try:
        exists = await owner.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", API_ROLE)
        if not exists:
            raise SystemExit(f"Role {API_ROLE} does not exist. Run: alembic upgrade head")
        await owner.execute(f"ALTER ROLE {API_ROLE} WITH LOGIN PASSWORD '{verifier}'")
    finally:
        await owner.close()

    url = api_database_url(settings.MIGRATION_DATABASE_URL, password)
    api = await connect_api(url)
    try:
        current_user = await api.fetchval("SELECT current_user")
    finally:
        await api.close()
    if current_user != API_ROLE:
        raise SystemExit(f"Unexpected login role {current_user!r}")

    if write_env:
        update_env_file("DATABASE_URL", url)
        print(f"OK: {API_ROLE} password set; DATABASE_URL written to {ENV_FILE}")
    else:
        print(f"OK: {API_ROLE} password set. Use this as DATABASE_URL:\n{url}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write-env", action="store_true", help="write DATABASE_URL into .env")
    args = parser.parse_args()
    asyncio.run(main(args.write_env))
