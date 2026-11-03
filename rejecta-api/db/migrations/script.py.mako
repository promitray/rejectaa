"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Raw SQL migration. Every new table MUST be passed through
db.migration_helpers.secure_table() so it gets RLS, the rejecta_api policy
and no access for Supabase's anon/authenticated/service_role.
"""

from collections.abc import Sequence

from db.migration_helpers import run_sql

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "run_sql([])"}


def downgrade() -> None:
    ${downgrades if downgrades else "run_sql([])"}
