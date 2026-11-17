"""The same model request gets the same answer: requests at temperature 0 are answered from the local store.

OpenAI's seed makes answers "mostly" repeatable, not always: the same paper could still get a different finding
on a second run. Keyed by the full request (model, every message, token limit, seed, format), a stored answer is
only reused for exactly the same request, so any change to the paper or to a prompt asks the model again.

Only usable answers are stored (valid JSON, not cut off at the token limit): a failed answer is never replayed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from config import settings
from services.local_store import digest, get_store

logger = logging.getLogger(__name__)
NAMESPACE = "llm"


def _is_json(text: Any) -> bool:
    try:
        return isinstance(json.loads(text), (dict, list))
    except (TypeError, ValueError):
        return False


def usable_text(value: Any) -> bool:
    return isinstance(value, str) and _is_json(value)


async def cached(request: dict[str, Any], call: Callable[[], Awaitable[Any]], usable: Callable[[Any], bool]) -> Any:
    """`call()`'s answer, from the store when exactly this request was answered before."""
    if not settings.LLM_RESPONSE_CACHE:
        return await call()
    store = get_store()
    key = digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
    found = await store.get(NAMESPACE, key)
    if found is not None and usable(found):
        return found
    answer = await call()
    if usable(answer):
        await store.put(NAMESPACE, key, answer)
    return answer
