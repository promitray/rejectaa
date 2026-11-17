"""Recognising a revised version of a paper the same account analysed before, and comparing the two.

For every analysis the API keeps a small record for HISTORY_TTL: a fingerprint of the text, the file name,
the journal, the scores (overall, criteria, sections) and the rejection reasons' problem statements. The
fingerprint is the smallest hashes of the paper's five-word sequences: enough to tell that two files share
most of their text, and nothing from which the text can be read back. No paper text is stored.

When a new upload shares enough of its text with an earlier one of the same account, it is treated as a
revision: the model is shown the earlier issues and says which are resolved, and the report puts the
earlier scores next to the new ones.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from models import AnalysisResult, PreviousIssue, RevisionComparison, ScoreChange
from services.documents.store import Owner

SHINGLE_WORDS = 5
SIGNATURE_SIZE = 128
# Share of five-word sequences two files must have in common to be versions of one paper. A revision after
# peer review keeps far more than this; two different papers by the same authors share almost none.
REVISION_SIMILARITY = 0.3
HISTORY_TTL = timedelta(days=180)
RECENT_LIMIT = 50  # earlier analyses of an account compared with a new upload
MAX_PROBLEM_CHARS = 300
MAX_JOURNAL_CHARS = 200  # the analysis_history column's limit (CHECK in migration 0010)
ISSUE_STATUSES = ("resolved", "partly_resolved", "open")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- fingerprint

def signature(text: str) -> list[int]:
    """The SIGNATURE_SIZE smallest hashes of the text's five-word sequences (a bottom-k MinHash sketch)."""
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).lower())
    hashes: set[int] = set()
    for start in range(max(0, len(words) - SHINGLE_WORDS + 1)):
        shingle = " ".join(words[start : start + SHINGLE_WORDS]).encode("utf-8")
        hashes.add(int.from_bytes(hashlib.blake2b(shingle, digest_size=8).digest(), "big") >> 1)  # fits a bigint
    return sorted(hashes)[:SIGNATURE_SIZE]


def similarity(a: list[int], b: list[int]) -> float:
    """Estimated share of five-word sequences the two texts have in common (Jaccard), from their sketches."""
    if not a or not b:
        return 0.0
    in_a, in_b = set(a), set(b)
    union = sorted(in_a | in_b)[:SIGNATURE_SIZE]
    return sum(1 for h in union if h in in_a and h in in_b) / len(union)


# --------------------------------------------------------------------------- records

@dataclass(frozen=True)
class PreviousAnalysis:
    id: uuid.UUID
    created_at: datetime
    filename: str
    journal: str
    article_type: str | None
    file_sha256: str
    signature: list[int]
    summary: dict[str, Any]
    similarity: float = 0.0
    same_file: bool = False

    def issues(self) -> list[dict[str, Any]]:
        """The earlier rejection reasons, numbered from 1, as the prompt shows them."""
        return [
            {"id": number, "severity": item.get("severity", "major"), "problem": item.get("problem", "")}
            for number, item in enumerate(self.summary.get("reasons") or [], start=1)
            if item.get("problem")
        ]


    def section_names(self) -> list[str]:
        """The sections the earlier version was divided into, in order (names only, never text)."""
        names = self.summary.get("sections") or {}
        return [name for name in names if isinstance(name, str)]


def summarize(analysis: AnalysisResult) -> dict[str, Any]:
    """What is kept of an analysis: scores and the reasons' problem statements, no text of the paper."""
    return {
        "score": analysis.mood_score,
        "criteria": {name: c.score for name, c in analysis.criteria.items()},
        "sections": {name: s.score for name, s in analysis.sections.items()},
        "reasons": [
            {"severity": r.severity.value, "problem": (r.problem or r.reason)[:MAX_PROBLEM_CHARS]}
            for r in analysis.rejection_reasons
        ],
    }


@dataclass(frozen=True)
class NewRecord:
    filename: str
    journal: str
    article_type: str | None
    file_sha256: str
    signature: list[int]
    summary: dict[str, Any]


class HistoryStore(Protocol):
    async def save(self, owner: Owner, record: NewRecord) -> uuid.UUID: ...
    async def recent(self, owner: Owner, limit: int = RECENT_LIMIT) -> list[PreviousAnalysis]: ...
    async def purge_expired(self) -> int: ...


async def find_previous(store: HistoryStore, owner: Owner, sig: list[int], file_sha256: str) -> PreviousAnalysis | None:
    """The most similar earlier analysis of this account that is a version of the same paper, or None."""
    best: PreviousAnalysis | None = None
    for entry in await store.recent(owner):
        same_file = entry.file_sha256 == file_sha256
        score = 1.0 if same_file else similarity(sig, entry.signature)
        if score >= REVISION_SIMILARITY and (best is None or score > best.similarity):
            best = replace(entry, similarity=round(score, 3), same_file=same_file)
    return best  # entries come newest first, so a tie keeps the latest version


# --------------------------------------------------------------------------- comparing

def _changes(before: dict[str, Any], after: dict[str, int]) -> list[ScoreChange]:
    names = list(after) + [name for name in before if name not in after]
    return [
        ScoreChange(name=name, before=before.get(name) if isinstance(before.get(name), int) else None, after=after.get(name))
        for name in names
    ]


def journal_key(journal: str) -> str:
    """A journal as it is kept in the history (stripped, cut to the column's length), for comparing two analyses."""
    return journal.strip()[:MAX_JOURNAL_CHARS].lower()


def compare(previous: PreviousAnalysis, analysis: AnalysisResult, journal: str) -> RevisionComparison:
    """The earlier analysis next to this one: scores, criteria, sections, and what became of the earlier issues.

    The very same file has no issues to resolve: nothing in the paper changed, so the earlier issues are not
    put to the model and not listed.
    """
    summary = previous.summary
    statuses = {item.id: item for item in analysis.previous_issues}
    issues = []
    for issue in [] if previous.same_file else previous.issues():
        found = statuses.get(issue["id"])
        issues.append(
            PreviousIssue(
                problem=issue["problem"],
                severity=issue["severity"],
                status=found.status if found else "not_assessed",
                note=found.note if found else "",
            )
        )
    return RevisionComparison(
        previous_analysis_id=str(previous.id),
        previous_analysed_at=previous.created_at.isoformat(),
        previous_filename=previous.filename,
        previous_journal=previous.journal,
        same_file=previous.same_file,
        same_journal=journal_key(previous.journal) == journal_key(journal),
        similarity=previous.similarity,
        score_before=int(summary.get("score") or 0),
        score_after=analysis.mood_score,
        criteria=_changes(summary.get("criteria") or {}, {k: c.score for k, c in analysis.criteria.items()}),
        sections=_changes(summary.get("sections") or {}, {k: s.score for k, s in analysis.sections.items()}),
        issues=issues,
    )


# --------------------------------------------------------------------------- stores

@dataclass
class _MemoryEntry:
    owner: Owner
    entry: PreviousAnalysis
    expires_at: datetime


@dataclass
class MemoryHistoryStore:
    """Same behaviour as the Postgres store, kept in memory: for tests and local runs without a database."""

    entries: list[_MemoryEntry] = field(default_factory=list)

    async def save(self, owner: Owner, record: NewRecord) -> uuid.UUID:
        entry = PreviousAnalysis(
            uuid.uuid4(), _now(), record.filename, record.journal, record.article_type, record.file_sha256,
            list(record.signature), json.loads(json.dumps(record.summary)),
        )
        self.entries.append(_MemoryEntry(owner, entry, _now() + HISTORY_TTL))
        return entry.id

    async def recent(self, owner: Owner, limit: int = RECENT_LIMIT) -> list[PreviousAnalysis]:
        now = _now()
        mine = [e.entry for e in self.entries if e.owner == owner and e.expires_at > now]
        return sorted(mine, key=lambda e: e.created_at, reverse=True)[:limit]

    async def purge_expired(self) -> int:
        now = _now()
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.expires_at > now]
        return before - len(self.entries)


class PostgresHistoryStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def save(self, owner: Owner, record: NewRecord) -> uuid.UUID:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO analysis_history
                       (owner_kind, owner_id, file_sha256, filename, journal, article_type, signature, summary, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, now() + make_interval(secs => $9::double precision))
                RETURNING id
                """,
                owner.kind, owner.id, record.file_sha256, record.filename[:300], record.journal[:MAX_JOURNAL_CHARS],
                record.article_type, record.signature, json.dumps(record.summary), HISTORY_TTL.total_seconds(),
            )

    async def recent(self, owner: Owner, limit: int = RECENT_LIMIT) -> list[PreviousAnalysis]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, created_at, filename, journal, article_type, file_sha256, signature, summary
                  FROM analysis_history
                 WHERE owner_kind = $1 AND owner_id = $2 AND expires_at > now()
                 ORDER BY created_at DESC
                 LIMIT $3
                """,
                owner.kind, owner.id, limit,
            )
        return [
            PreviousAnalysis(
                row["id"], row["created_at"], row["filename"], row["journal"], row["article_type"], row["file_sha256"],
                list(row["signature"] or []),
                json.loads(row["summary"]) if isinstance(row["summary"], str) else dict(row["summary"]),
            )
            for row in rows
        ]

    async def purge_expired(self) -> int:
        async with self.pool.acquire() as conn:
            status = await conn.execute("DELETE FROM analysis_history WHERE expires_at <= now()")
        return int(status.split()[-1]) if status and status.split()[-1].isdigit() else 0
