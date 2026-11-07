"""Shared cache of author profiles (ORCID -> publication record).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06

Every author an user looks up is stored here, keyed by ORCID, so the next
lookup (by anybody) is answered from the database instead of ORCID and
OpenAlex. Rows hold only public scholarly data. ``expires_at`` says when the
row should be refreshed; an expired row is still served if the upstream
sources are down. ``hit_count`` shows how often the cache saved a lookup.
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, run_sql, secure_table

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    run_sql(
        [
            f"""
            CREATE TABLE {S}.author_profiles (
                orcid             text PRIMARY KEY
                                  CHECK (orcid ~ '^[0-9]{{4}}-[0-9]{{4}}-[0-9]{{4}}-[0-9]{{3}}[0-9X]$'),
                status            text NOT NULL CHECK (status IN ('found', 'not_found')),
                display_name      text CHECK (display_name IS NULL OR length(display_name) <= 300),
                payload           jsonb,
                fetched_at        timestamptz NOT NULL DEFAULT now(),
                expires_at        timestamptz NOT NULL,
                hit_count         integer NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
                last_requested_at timestamptz NOT NULL DEFAULT now(),
                CHECK ((status = 'found') = (payload IS NOT NULL))
            )
            """,
            f"CREATE INDEX author_profiles_expires_at_idx ON {S}.author_profiles (expires_at)",
            f"CREATE INDEX author_profiles_last_requested_idx ON {S}.author_profiles (last_requested_at DESC)",
            *secure_table("author_profiles"),
        ]
    )


def downgrade() -> None:
    run_sql([f"DROP TABLE IF EXISTS {S}.author_profiles"])
