"""Where working copies live: Postgres in the running API, memory in tests.

Every call takes the owner, and a document is only ever found for its owner, so one account can never
read, change or download another account's copy (a wrong owner looks exactly like a missing document).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

KEEP_VERSIONS = 20  # older versions are dropped; undo goes back at most this far
DOCUMENT_TTL = timedelta(hours=24)


class VersionConflict(Exception):
    """The document changed since the caller read it."""


@dataclass(frozen=True)
class Owner:
    kind: str  # 'user' | 'client'
    id: uuid.UUID


@dataclass(frozen=True)
class StoredDocument:
    id: uuid.UUID
    filename: str
    source_format: str
    version: int
    docx: bytes
    sections: list[dict[str, Any]]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class StoredProposal:
    id: uuid.UUID
    document_id: uuid.UUID
    section_id: str
    base_version: int
    status: str
    payload: dict[str, Any]


class DocumentStore(Protocol):
    async def create(self, owner: Owner, filename: str, source_format: str, docx: bytes, sections: list[dict[str, Any]]) -> StoredDocument: ...
    async def get(self, owner: Owner, document_id: uuid.UUID) -> StoredDocument | None: ...
    async def add_version(
        self, owner: Owner, document_id: uuid.UUID, expected_version: int, docx: bytes, sections: list[dict[str, Any]], note: str
    ) -> StoredDocument: ...
    async def drop_latest_version(self, owner: Owner, document_id: uuid.UUID) -> StoredDocument | None: ...
    async def delete(self, owner: Owner, document_id: uuid.UUID) -> bool: ...
    async def save_proposal(self, owner: Owner, document_id: uuid.UUID, section_id: str, base_version: int, payload: dict[str, Any]) -> uuid.UUID: ...
    async def get_proposal(self, owner: Owner, document_id: uuid.UUID, proposal_id: uuid.UUID) -> StoredProposal | None: ...
    async def set_proposal_status(self, owner: Owner, document_id: uuid.UUID, proposal_id: uuid.UUID, status: str) -> None: ...
    async def purge_expired(self) -> int: ...


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- memory (tests)

@dataclass
class _MemoryDocument:
    owner: Owner
    filename: str
    source_format: str
    created_at: datetime
    expires_at: datetime
    versions: list[tuple[int, bytes, list[dict[str, Any]]]] = field(default_factory=list)


class MemoryDocumentStore:
    """Same behaviour as the Postgres store, kept in a dict. For tests and local runs without a database."""

    def __init__(self) -> None:
        self.documents: dict[uuid.UUID, _MemoryDocument] = {}
        self.proposals: dict[uuid.UUID, StoredProposal] = {}

    def _find(self, owner: Owner, document_id: uuid.UUID) -> _MemoryDocument | None:
        doc = self.documents.get(document_id)
        if doc is None or doc.owner != owner or doc.expires_at <= _now():
            return None
        return doc

    def _stored(self, document_id: uuid.UUID, doc: _MemoryDocument) -> StoredDocument:
        version, docx, sections = doc.versions[-1]
        return StoredDocument(document_id, doc.filename, doc.source_format, version, docx, json.loads(json.dumps(sections)), doc.created_at, doc.expires_at)

    async def create(self, owner, filename, source_format, docx, sections):
        document_id = uuid.uuid4()
        now = _now()
        self.documents[document_id] = _MemoryDocument(owner, filename, source_format, now, now + DOCUMENT_TTL, [(1, docx, sections)])
        return self._stored(document_id, self.documents[document_id])

    async def get(self, owner, document_id):
        doc = self._find(owner, document_id)
        return self._stored(document_id, doc) if doc else None

    async def add_version(self, owner, document_id, expected_version, docx, sections, note):
        doc = self._find(owner, document_id)
        if doc is None:
            raise KeyError(document_id)
        if doc.versions[-1][0] != expected_version:
            raise VersionConflict()
        doc.versions.append((expected_version + 1, docx, sections))
        doc.versions = doc.versions[-KEEP_VERSIONS:]
        return self._stored(document_id, doc)

    async def drop_latest_version(self, owner, document_id):
        doc = self._find(owner, document_id)
        if doc is None or len(doc.versions) < 2:
            return None
        doc.versions.pop()
        return self._stored(document_id, doc)

    async def delete(self, owner, document_id):
        if self._find(owner, document_id) is None:
            return False
        del self.documents[document_id]
        self.proposals = {k: v for k, v in self.proposals.items() if v.document_id != document_id}
        return True

    async def save_proposal(self, owner, document_id, section_id, base_version, payload):
        if self._find(owner, document_id) is None:
            raise KeyError(document_id)
        proposal_id = uuid.uuid4()
        self.proposals[proposal_id] = StoredProposal(proposal_id, document_id, section_id, base_version, "open", payload)
        return proposal_id

    async def get_proposal(self, owner, document_id, proposal_id):
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.document_id != document_id or self._find(owner, document_id) is None:
            return None
        return proposal

    async def set_proposal_status(self, owner, document_id, proposal_id, status):
        proposal = await self.get_proposal(owner, document_id, proposal_id)
        if proposal is not None:
            self.proposals[proposal_id] = replace(proposal, status=status)

    async def purge_expired(self):
        expired = [k for k, v in self.documents.items() if v.expires_at <= _now()]
        for key in expired:
            del self.documents[key]
        return len(expired)


# --------------------------------------------------------------------------- Postgres

class PostgresDocumentStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    @staticmethod
    def _sections(value: Any) -> list[dict[str, Any]]:
        return json.loads(value) if isinstance(value, str) else list(value)

    async def _load(self, conn, owner: Owner, document_id: uuid.UUID) -> StoredDocument | None:
        row = await conn.fetchrow(
            """
            SELECT d.id, d.filename, d.source_format, d.current_version, d.created_at, d.expires_at, v.docx, v.sections
              FROM documents d
              JOIN document_versions v ON v.document_id = d.id AND v.version = d.current_version
             WHERE d.id = $1 AND d.owner_kind = $2 AND d.owner_id = $3 AND d.expires_at > now()
            """,
            document_id, owner.kind, owner.id,
        )
        if row is None:
            return None
        return StoredDocument(
            row["id"], row["filename"], row["source_format"], row["current_version"], bytes(row["docx"]),
            self._sections(row["sections"]), row["created_at"], row["expires_at"],
        )

    async def create(self, owner, filename, source_format, docx, sections):
        async with self.pool.acquire() as conn, conn.transaction():
            document_id = await conn.fetchval(
                """
                INSERT INTO documents (owner_kind, owner_id, filename, source_format, expires_at)
                VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5::double precision))
                RETURNING id
                """,
                owner.kind, owner.id, filename[:300], source_format, DOCUMENT_TTL.total_seconds(),
            )
            await conn.execute(
                "INSERT INTO document_versions (document_id, version, docx, sections, note) VALUES ($1, 1, $2, $3::jsonb, 'original')",
                document_id, docx, json.dumps(sections),
            )
            return await self._load(conn, owner, document_id)

    async def get(self, owner, document_id):
        async with self.pool.acquire() as conn:
            return await self._load(conn, owner, document_id)

    async def add_version(self, owner, document_id, expected_version, docx, sections, note):
        async with self.pool.acquire() as conn, conn.transaction():
            moved = await conn.fetchval(
                """
                UPDATE documents SET current_version = current_version + 1
                 WHERE id = $1 AND owner_kind = $2 AND owner_id = $3 AND current_version = $4 AND expires_at > now()
                RETURNING current_version
                """,
                document_id, owner.kind, owner.id, expected_version,
            )
            if moved is None:
                raise VersionConflict()
            # an undo may have left a newer version row behind; this version replaces it
            await conn.execute("DELETE FROM document_versions WHERE document_id = $1 AND version >= $2", document_id, moved)
            await conn.execute(
                "INSERT INTO document_versions (document_id, version, docx, sections, note) VALUES ($1, $2, $3, $4::jsonb, $5)",
                document_id, moved, docx, json.dumps(sections), note[:300],
            )
            await conn.execute(
                "DELETE FROM document_versions WHERE document_id = $1 AND version <= $2", document_id, moved - KEEP_VERSIONS
            )
            return await self._load(conn, owner, document_id)

    async def drop_latest_version(self, owner, document_id):
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchval(
                """
                SELECT current_version FROM documents
                 WHERE id = $1 AND owner_kind = $2 AND owner_id = $3 AND expires_at > now() FOR UPDATE
                """,
                document_id, owner.kind, owner.id,
            )
            if current is None:
                return None
            previous = await conn.fetchval(
                "SELECT max(version) FROM document_versions WHERE document_id = $1 AND version < $2", document_id, current
            )
            if previous is None:
                return None
            await conn.execute("DELETE FROM document_versions WHERE document_id = $1 AND version = $2", document_id, current)
            await conn.execute("UPDATE documents SET current_version = $2 WHERE id = $1", document_id, previous)
            return await self._load(conn, owner, document_id)

    async def delete(self, owner, document_id):
        async with self.pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM documents WHERE id = $1 AND owner_kind = $2 AND owner_id = $3 RETURNING id",
                document_id, owner.kind, owner.id,
            )
            return deleted is not None

    async def save_proposal(self, owner, document_id, section_id, base_version, payload):
        async with self.pool.acquire() as conn:
            proposal_id = await conn.fetchval(
                """
                INSERT INTO document_proposals (document_id, section_id, base_version, payload)
                SELECT id, $4, $5, $6::jsonb FROM documents
                 WHERE id = $1 AND owner_kind = $2 AND owner_id = $3 AND expires_at > now()
                RETURNING id
                """,
                document_id, owner.kind, owner.id, section_id, base_version, json.dumps(payload),
            )
            if proposal_id is None:
                raise KeyError(document_id)
            return proposal_id

    async def get_proposal(self, owner, document_id, proposal_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.id, p.document_id, p.section_id, p.base_version, p.status, p.payload
                  FROM document_proposals p JOIN documents d ON d.id = p.document_id
                 WHERE p.id = $1 AND p.document_id = $2 AND d.owner_kind = $3 AND d.owner_id = $4 AND d.expires_at > now()
                """,
                proposal_id, document_id, owner.kind, owner.id,
            )
        if row is None:
            return None
        payload = row["payload"]
        return StoredProposal(
            row["id"], row["document_id"], row["section_id"], row["base_version"], row["status"],
            json.loads(payload) if isinstance(payload, str) else payload,
        )

    async def set_proposal_status(self, owner, document_id, proposal_id, status):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE document_proposals p SET status = $5
                  FROM documents d
                 WHERE p.id = $1 AND p.document_id = $2 AND d.id = p.document_id AND d.owner_kind = $3 AND d.owner_id = $4
                """,
                proposal_id, document_id, owner.kind, owner.id, status,
            )

    async def purge_expired(self):
        async with self.pool.acquire() as conn:
            result = await conn.execute("DELETE FROM documents WHERE expires_at <= now()")
        return int(result.split()[-1]) if result else 0
