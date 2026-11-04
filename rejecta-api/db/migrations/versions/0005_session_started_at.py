"""Absolute session lifetime for refresh-token families.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03

Every rotated refresh token copies ``session_started_at`` from the token it
replaces, so a session cannot be kept alive forever by refreshing:
SESSION_MAX_LIFETIME_DAYS after the original login, the user must log in again.
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, run_sql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    run_sql(
        [
            f"""
            ALTER TABLE {S}.refresh_tokens
                ADD COLUMN session_started_at timestamptz NOT NULL DEFAULT now()
            """,
            f"""
            ALTER TABLE {S}.refresh_tokens
                ADD CONSTRAINT refresh_tokens_started_before_expiry
                CHECK (session_started_at < expires_at)
            """,
        ]
    )


def downgrade() -> None:
    run_sql(
        [
            f"ALTER TABLE {S}.refresh_tokens DROP CONSTRAINT IF EXISTS refresh_tokens_started_before_expiry",
            f"ALTER TABLE {S}.refresh_tokens DROP COLUMN IF EXISTS session_started_at",
        ]
    )
