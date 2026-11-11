"""Working copies of manuscripts (opt-in, kept 24 hours).

A user (or a partner client) who asks for it gets an editable Word copy of the uploaded paper, so that
calibrated sections can be written back into it. ``documents`` is the copy, ``document_versions`` keeps
each saved state (for undo and download), ``document_proposals`` holds calibrated text waiting for the
user to apply it. Rows expire after ``expires_at`` and are deleted by the API; deleting a document
deletes its versions and proposals.

revision: 0009
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, run_sql, secure_table

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    run_sql(
        [
            f"""
            CREATE TABLE {S}.documents (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                owner_kind      text NOT NULL CHECK (owner_kind IN ('user', 'client')),
                owner_id        uuid NOT NULL,
                filename        text NOT NULL CHECK (length(filename) <= 300),
                source_format   text NOT NULL CHECK (source_format IN ('pdf', 'docx')),
                current_version integer NOT NULL DEFAULT 1 CHECK (current_version >= 1),
                created_at      timestamptz NOT NULL DEFAULT now(),
                expires_at      timestamptz NOT NULL
            )
            """,
            f"CREATE INDEX documents_owner_idx ON {S}.documents (owner_kind, owner_id)",
            f"CREATE INDEX documents_expires_at_idx ON {S}.documents (expires_at)",
            *secure_table("documents"),
            f"""
            CREATE TABLE {S}.document_versions (
                document_id uuid NOT NULL REFERENCES {S}.documents (id) ON DELETE CASCADE,
                version     integer NOT NULL CHECK (version >= 1),
                docx        bytea NOT NULL CHECK (octet_length(docx) <= 20000000),
                sections    jsonb NOT NULL,
                note        text NOT NULL DEFAULT '' CHECK (length(note) <= 300),
                created_at  timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (document_id, version)
            )
            """,
            *secure_table("document_versions"),
            f"""
            CREATE TABLE {S}.document_proposals (
                id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                document_id  uuid NOT NULL REFERENCES {S}.documents (id) ON DELETE CASCADE,
                section_id   text NOT NULL CHECK (length(section_id) <= 20),
                base_version integer NOT NULL,
                status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected', 'stale')),
                payload      jsonb NOT NULL,
                created_at   timestamptz NOT NULL DEFAULT now()
            )
            """,
            f"CREATE INDEX document_proposals_document_idx ON {S}.document_proposals (document_id)",
            *secure_table("document_proposals"),
        ]
    )


def downgrade() -> None:
    run_sql(
        [
            f"DROP TABLE IF EXISTS {S}.document_proposals",
            f"DROP TABLE IF EXISTS {S}.document_versions",
            f"DROP TABLE IF EXISTS {S}.documents",
        ]
    )
