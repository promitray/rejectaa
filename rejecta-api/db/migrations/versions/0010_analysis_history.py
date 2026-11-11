"""Analysis history: a short record of each paper analysis, so a revised version can be recognised.

One row per analysis, kept 180 days: the file's SHA-256, file name, journal, article type, a fingerprint of
the text (the 128 smallest hashes of its five-word sequences; the text cannot be rebuilt from it), and a
summary of the result (scores and the rejection reasons' problem statements). No text of the paper is
stored. Rows expire after ``expires_at`` and are deleted by the API.

revision: 0010
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, run_sql, secure_table

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    run_sql(
        [
            f"""
            CREATE TABLE {S}.analysis_history (
                id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                owner_kind   text NOT NULL CHECK (owner_kind IN ('user', 'client')),
                owner_id     uuid NOT NULL,
                file_sha256  text NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{{64}}$'),
                filename     text NOT NULL CHECK (length(filename) <= 300),
                journal      text NOT NULL CHECK (length(journal) <= 200),
                article_type text CHECK (article_type IS NULL OR length(article_type) <= 40),
                signature    bigint[] NOT NULL CHECK (cardinality(signature) <= 256),
                summary      jsonb NOT NULL CHECK (octet_length(summary::text) <= 20000),
                created_at   timestamptz NOT NULL DEFAULT now(),
                expires_at   timestamptz NOT NULL
            )
            """,
            f"CREATE INDEX analysis_history_owner_idx ON {S}.analysis_history (owner_kind, owner_id, created_at DESC)",
            f"CREATE INDEX analysis_history_expires_at_idx ON {S}.analysis_history (expires_at)",
            *secure_table("analysis_history"),
        ]
    )


def downgrade() -> None:
    run_sql([f"DROP TABLE IF EXISTS {S}.analysis_history"])
