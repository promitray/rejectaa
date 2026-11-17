"""The same paper gets the same lookups: citation checks, the journal record and the publication record are
kept per paper for a few days.

These lookups go over the network. When a service is busy, a second run of the same paper can verify one more
reference or miss the journal, and since their results are part of what the model is given, the whole report
could change. A snapshot of the first answer is kept in the local store (services.local_store) and reused for
SNAPSHOT_DAYS, so a re-run of an unchanged paper is given exactly the same facts. "Run again" in the app asks for
fresh lookups.

What is kept: the citation results always (a reference that could not be checked stays "not checked" until a
fresh run); the journal record only when it was found; the publication record only when it was checked.
Never raises: without a store the lookups simply run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from services.local_store import digest, get_store

logger = logging.getLogger(__name__)

SNAPSHOT_DAYS = 7
NAMESPACE = "lookups"
T = TypeVar("T")


async def snapshot(
    kind: str,
    key_parts: list[str],
    fetch: Callable[[], Awaitable[T]],
    dump: Callable[[T], Any],
    load: Callable[[Any], T],
    keep: Callable[[T], bool] = lambda _: True,
    refresh: bool = False,
) -> T:
    """`fetch()`'s result, or the one kept for the same key within SNAPSHOT_DAYS. Errors from `fetch` propagate
    (the pipeline already handles a failed lookup); a store failure only means the lookup runs."""
    store = get_store()
    key = digest(kind, *key_parts)
    if not refresh:
        found = await store.get(NAMESPACE, key)
        if isinstance(found, dict) and time.time() - float(found.get("at") or 0) < SNAPSHOT_DAYS * 86400:
            try:
                return load(found["value"])
            except Exception:  # noqa: BLE001 - an entry from an older format is fetched again
                logger.info("Lookup snapshot for %s could not be read; fetching again", kind)
    result = await fetch()
    try:
        if keep(result):
            await store.put(NAMESPACE, key, {"at": time.time(), "value": dump(result)})
    except Exception:  # noqa: BLE001
        logger.warning("Could not keep the %s lookup", kind, exc_info=True)
    return result
