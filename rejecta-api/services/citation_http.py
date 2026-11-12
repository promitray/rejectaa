"""HTTP plumbing for citation lookups: shared per-service pacing, retries, cache, and failure tracking.

Why this exists: a paper with 40 references used to fire dozens of requests at once. Services such as
CrossRef, OpenAlex and Semantic Scholar answer that with HTTP 429, and the old code treated every
non-200 answer as "reference not found". A reference that could not be CHECKED looked the same as a
reference that does not EXIST. This module keeps the two apart:

- one limiter per service, shared by every reference in the process (not per reference)
- 429/503 honour Retry-After; a long Retry-After pauses that service for everyone instead of
  hammering it, and callers see "busy", not "not found"
- a Trace per reference records what each service said, so the final verdict can say
  "could not complete the check" instead of "not found"
- results are cached, so the same reference (in this paper or the next) is not looked up twice
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Outcome kinds. "busy" and "error" mean the question was NOT answered.
OK = "ok"
NOT_FOUND = "not_found"
BUSY = "busy"
ERROR = "error"

RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRY_WAIT_SECONDS = 6.0  # longer Retry-After than this: pause the service instead of waiting here
MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
MAX_BLOCK_SECONDS = 120.0  # never trust a Retry-After longer than this
COOLDOWN_AFTER_FAILURE_SECONDS = 4.0  # after retries are used up, others wait this long instead of retrying too


@dataclass
class Fetch:
    """Result of one polite request."""

    kind: str  # OK, NOT_FOUND, BUSY or ERROR
    response: httpx.Response | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == OK


class SourceLimiter:
    """Concurrency + spacing + shared back-off for one external service."""

    def __init__(self, name: str, concurrency: int, gap_seconds: float) -> None:
        self.name = name
        self.gap_seconds = gap_seconds
        self._gate = asyncio.Semaphore(concurrency)
        self._blocked_until = 0.0  # monotonic time before which the service must not be called

    def blocked_for(self) -> float:
        return max(0.0, self._blocked_until - time.monotonic())

    def block(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + min(seconds, MAX_BLOCK_SECONDS))

    async def get(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 8.0,
    ) -> Fetch:
        """GET with pacing and retries. Never raises for HTTP or network problems."""
        last_detail = ""
        for attempt in range(MAX_ATTEMPTS):
            remaining = self.blocked_for()
            if remaining > MAX_RETRY_WAIT_SECONDS:
                return Fetch(BUSY, None, f"{self.name} is rate-limiting requests (retry in {remaining:.0f}s)")
            if remaining > 0:
                await asyncio.sleep(remaining)

            async with self._gate:
                # A 429 may have arrived while this request waited for a slot.
                remaining = self.blocked_for()
                if remaining > MAX_RETRY_WAIT_SECONDS:
                    return Fetch(BUSY, None, f"{self.name} is rate-limiting requests (retry in {remaining:.0f}s)")
                if remaining > 0:
                    await asyncio.sleep(remaining)
                try:
                    response = await client.get(url, params=params, headers=headers, timeout=timeout)
                except httpx.TimeoutException:
                    last_detail = f"{self.name} timed out"
                    await asyncio.sleep(self.gap_seconds)
                    continue
                except httpx.RequestError as exc:
                    return Fetch(ERROR, None, f"{self.name} unreachable: {type(exc).__name__}")
                await asyncio.sleep(self.gap_seconds)

            status = response.status_code
            if status == 200:
                return Fetch(OK, response)
            if status == 404:
                return Fetch(NOT_FOUND, response)
            if status in RETRY_STATUSES:
                wait = _retry_after(response, DEFAULT_BACKOFF_SECONDS * (attempt + 1))
                last_detail = f"{self.name} answered HTTP {status}"
                if wait > MAX_RETRY_WAIT_SECONDS:
                    self.block(wait)
                    return Fetch(BUSY, response, f"{self.name} is rate-limiting requests (retry in {wait:.0f}s)")
                await asyncio.sleep(wait)
                continue
            return Fetch(ERROR, response, f"{self.name} answered HTTP {status}")

        # Retries are used up: a short shared cooldown stops every other reference from repeating
        # the same doomed retries against a service that is struggling.
        self.block(COOLDOWN_AFTER_FAILURE_SECONDS)
        return Fetch(BUSY, None, last_detail or f"{self.name} did not answer in time")


def _retry_after(response: httpx.Response, default: float) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        return default


# Pacing is a polite fraction of each service's published limits.
#   CrossRef: ~5 requests/second without a key, more with a polite User-Agent/mailto.
#   Europe PMC: generous, no key.
#   OpenAlex: 10 requests/second with an API key; without one, anonymous search is throttled hard.
#   arXiv: asks for one request every few seconds.
_SOURCES: dict[str, tuple[int, float]] = {
    "CrossRef": (3, 0.2),
    "Europe PMC": (3, 0.1),
    "OpenAlex": (3, 0.12),
    "arXiv": (2, 0.5),
    "DataCite": (3, 0.1),
    # The publication-status check (services.integrity) makes one to three requests per paper. It has its own
    # lanes so it never waits behind the reference list, which may hold 150 lookups for the same services.
    "CrossRef status": (2, 0.1),
    "OpenAlex status": (1, 0.1),
    "Cellosaurus": (2, 0.1),
}
_ANONYMOUS_OPENALEX = (1, 1.0)

_limiters: dict[str, SourceLimiter] = {}
_limiters_loop_id: int | None = None


def limiter(name: str, *, anonymous: bool = False) -> SourceLimiter:
    """The shared limiter for a service in the running event loop.

    Semaphores belong to one event loop, so limiters are rebuilt if the loop changes (tests).
    """
    global _limiters_loop_id
    loop_id = id(asyncio.get_running_loop())
    if loop_id != _limiters_loop_id:
        _limiters.clear()
        _limiters_loop_id = loop_id
    key = f"{name}:anon" if anonymous else name
    if key not in _limiters:
        concurrency, gap = _ANONYMOUS_OPENALEX if anonymous and name == "OpenAlex" else _SOURCES[name]
        _limiters[key] = SourceLimiter(name, concurrency, gap)
    return _limiters[key]


def reset_limiters() -> None:
    global _limiters_loop_id
    _limiters.clear()
    _limiters_loop_id = None


@dataclass
class Trace:
    """What each service said about one reference."""

    outcomes: list[tuple[str, str, str]] = field(default_factory=list)  # (source, kind, detail)
    closest: str | None = None  # note about the best non-matching record, shown when unverified
    _closest_score: int = -1

    def offer_closest(self, score: int, text: str) -> None:
        """Remember the most similar record that still failed the fingerprint check."""
        if score > self._closest_score:
            self._closest_score = score
            self.closest = text

    def note(self, source: str, fetch_or_kind: Fetch | str, detail: str = "") -> None:
        if isinstance(fetch_or_kind, Fetch):
            self.outcomes.append((source, fetch_or_kind.kind, fetch_or_kind.detail))
        else:
            self.outcomes.append((source, fetch_or_kind, detail))

    @property
    def incomplete(self) -> bool:
        """True if at least one service could not answer. The reference may still exist."""
        return any(kind in (BUSY, ERROR) for _, kind, _ in self.outcomes)

    @property
    def answered(self) -> bool:
        return any(kind in (OK, NOT_FOUND) for _, kind, _ in self.outcomes)

    def explain(self) -> str:
        problems = []
        for source, kind, detail in self.outcomes:
            if kind in (BUSY, ERROR):
                problems.append(detail or f"{source} did not answer")
        return "; ".join(dict.fromkeys(problems))


class TTLCache:
    """Small in-memory cache with expiry. One API instance, so memory is enough."""

    def __init__(self, max_entries: int = 2000) -> None:
        self.max_entries = max_entries
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.monotonic():
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._items[key] = (time.monotonic() + ttl_seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
