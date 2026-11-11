"""The same file, analysed again with the same settings, gets the same report.

Results are kept in this process's memory only (nothing is written to disk or the database), for CACHE_TTL
and at most MAX_ENTRIES, keyed by the account, the file's SHA-256, the journal, mode and article type, the
prompt version and the model. A restart empties it. "Run again" in the app asks for a fresh analysis.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

from models import AnalysisPaperResponse
from services.documents.store import Owner
from services.llm.base import PROMPT_VERSION

CACHE_TTL_SECONDS = 7 * 24 * 3600.0
MAX_ENTRIES = 200

_entries: OrderedDict[str, tuple[float, AnalysisPaperResponse]] = OrderedDict()


def cache_key(owner: Owner, file_sha256: str, journal: str, mode: str, article_type: str | None, model: str) -> str:
    parts = [owner.kind, str(owner.id), file_sha256, journal.strip().lower(), mode, article_type or "auto", PROMPT_VERSION, model]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def get(key: str) -> AnalysisPaperResponse | None:
    found = _entries.get(key)
    if found is None:
        return None
    stored_at, response = found
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        del _entries[key]
        return None
    _entries.move_to_end(key)
    return response.model_copy(deep=True)


def put(key: str, response: AnalysisPaperResponse) -> None:
    # the working copy belongs to one request; a later request makes its own
    _entries[key] = (time.monotonic(), response.model_copy(deep=True, update={"document": None, "document_note": None}))
    _entries.move_to_end(key)
    while len(_entries) > MAX_ENTRIES:
        _entries.popitem(last=False)


def clear() -> None:
    _entries.clear()
