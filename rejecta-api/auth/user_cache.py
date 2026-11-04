"""Short-lived per-process cache of AuthState, keyed by user id.

Every authenticated request needs the user's current status, role and
token_version. Caching for AUTH_USER_CACHE_SECONDS (default 30s) avoids a
query per request. Any code path that changes a user calls ``invalidate``,
so changes made through the API take effect on the next request; changes
made directly in the database take effect within the TTL.
"""

import time
import uuid

from db.pool import get_pool
from db.repos import users
from db.repos.users import AuthState
from config import settings

MAX_ENTRIES = 10_000

_cache: dict[uuid.UUID, tuple[float, AuthState | None]] = {}


async def get(user_id: uuid.UUID) -> AuthState | None:
    now = time.monotonic()
    ttl = settings.AUTH_USER_CACHE_SECONDS
    hit = _cache.get(user_id)
    if hit is not None and ttl > 0 and hit[0] > now:
        return hit[1]

    async with get_pool().acquire() as conn:
        state = await users.get_auth_state(conn, user_id)

    if ttl > 0:
        if len(_cache) >= MAX_ENTRIES:
            _cache.clear()
        _cache[user_id] = (now + ttl, state)
    return state


def invalidate(user_id: uuid.UUID) -> None:
    _cache.pop(user_id, None)


def clear() -> None:
    _cache.clear()
