"""Pending TOTP secret during admin MFA enrollment.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03

Enrollment is two steps (show QR, then confirm a code). The secret shown in
step one is kept here, encrypted, until it is confirmed. Only then is it
moved to ``mfa_secret_enc`` and ``mfa_enabled`` set, which bumps
token_version (trigger from 0002). A half-finished enrollment therefore
never enables MFA and never signs anybody out.
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, run_sql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    run_sql(
        [
            f"ALTER TABLE {S}.users ADD COLUMN mfa_pending_secret_enc text",
            f"ALTER TABLE {S}.users ADD COLUMN mfa_pending_created_at timestamptz",
            f"""
            ALTER TABLE {S}.users ADD CONSTRAINT users_mfa_pending_pair
                CHECK ((mfa_pending_secret_enc IS NULL) = (mfa_pending_created_at IS NULL))
            """,
        ]
    )


def downgrade() -> None:
    run_sql(
        [
            f"ALTER TABLE {S}.users DROP CONSTRAINT IF EXISTS users_mfa_pending_pair",
            f"ALTER TABLE {S}.users DROP COLUMN IF EXISTS mfa_pending_created_at",
            f"ALTER TABLE {S}.users DROP COLUMN IF EXISTS mfa_pending_secret_enc",
        ]
    )
