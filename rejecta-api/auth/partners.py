"""Partner API clients: secrets and the per-process client cache.

Secrets are ``rjs_`` + 256 random bits. Only SHA-256 is stored (these are
high-entropy random values, so a slow hash adds nothing). The prefix and
last 4 characters are kept so admins can tell secrets apart.
"""

import hashlib
import secrets
import time
import uuid

from config import settings
from db.pool import get_pool
from db.repos import partners
from db.repos.partners import ClientState

CLIENT_ID_PREFIX = "rjc_"
SECRET_PREFIX = "rjs_"
MAX_ACTIVE_SECRETS = 2

_cache: dict[uuid.UUID, tuple[float, ClientState | None]] = {}


def new_client_id() -> str:
    return CLIENT_ID_PREFIX + secrets.token_urlsafe(18)


def new_secret() -> tuple[str, bytes, str, str]:
    """(secret, sha256, display prefix, last 4)."""
    raw = SECRET_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_secret(raw), raw[:8], raw[-4:]


def hash_secret(raw: str) -> bytes:
    return hashlib.sha256(raw.encode("utf-8")).digest()


async def get_client(client_uuid: uuid.UUID) -> ClientState | None:
    now = time.monotonic()
    ttl = settings.AUTH_USER_CACHE_SECONDS
    hit = _cache.get(client_uuid)
    if hit is not None and ttl > 0 and hit[0] > now:
        return hit[1]
    async with get_pool().acquire() as conn:
        state = await partners.get_state(conn, client_uuid)
    if ttl > 0:
        _cache[client_uuid] = (now + ttl, state)
    return state


def invalidate(client_uuid: uuid.UUID | None = None) -> None:
    if client_uuid is None:
        _cache.clear()
    else:
        _cache.pop(client_uuid, None)
