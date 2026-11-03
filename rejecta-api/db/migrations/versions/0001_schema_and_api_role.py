"""Schema app, least-privilege API role, baseline lockdown.

Revision ID: 0001
Revises:
Create Date: 2026-11-03

Creates the ``rejecta_api`` role as NOLOGIN. Its password is set separately
with ``python -m scripts.db_role`` so no secret ever lives in a migration.
"""

from collections.abc import Sequence

from db.migration_helpers import (
    API_ROLE,
    APP_SCHEMA,
    internal_function,
    revoke_from_supabase_roles,
    run_sql,
)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    run_sql(
        [
            f"CREATE SCHEMA IF NOT EXISTS {APP_SCHEMA}",
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{API_ROLE}') THEN
                    CREATE ROLE {API_ROLE}
                        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                        NOINHERIT NOREPLICATION NOBYPASSRLS
                        CONNECTION LIMIT 25;
                END IF;
            END
            $$
            """,
            # Session defaults for every API connection.
            f"ALTER ROLE {API_ROLE} SET search_path = {APP_SCHEMA}",
            f"ALTER ROLE {API_ROLE} SET statement_timeout = '15s'",
            f"ALTER ROLE {API_ROLE} SET idle_in_transaction_session_timeout = '30s'",
            f"ALTER ROLE {API_ROLE} SET lock_timeout = '5s'",
            # Nobody but the API role may even look inside the schema.
            f"REVOKE ALL ON SCHEMA {APP_SCHEMA} FROM PUBLIC",
            revoke_from_supabase_roles(f"SCHEMA {APP_SCHEMA}"),
            f"GRANT USAGE ON SCHEMA {APP_SCHEMA} TO {API_ROLE}",
            # The version table is created by Alembic before this migration;
            # make sure the API role can never rewrite migration history.
            f"REVOKE ALL ON TABLE {APP_SCHEMA}.alembic_version FROM PUBLIC",
            revoke_from_supabase_roles(f"TABLE {APP_SCHEMA}.alembic_version"),
            f"GRANT SELECT ON TABLE {APP_SCHEMA}.alembic_version TO {API_ROLE}",
            # Shared trigger function for updated_at columns.
            f"""
            CREATE OR REPLACE FUNCTION {APP_SCHEMA}.set_updated_at()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog
            AS $$
            BEGIN
                NEW.updated_at := now();
                RETURN NEW;
            END
            $$
            """,
            *internal_function("set_updated_at()"),
        ]
    )


def downgrade() -> None:
    run_sql(
        [
            f"DROP FUNCTION IF EXISTS {APP_SCHEMA}.set_updated_at()",
            # rejecta_api owns nothing, so revoking its grants is enough to drop
            # it. (DROP OWNED BY is not allowed for Supabase's postgres role on
            # PG16+, which has ADMIN but not SET on roles it created.)
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{API_ROLE}') THEN
                    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA {APP_SCHEMA} FROM {API_ROLE}';
                    EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA {APP_SCHEMA} FROM {API_ROLE}';
                    EXECUTE 'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {APP_SCHEMA} FROM {API_ROLE}';
                    EXECUTE 'REVOKE ALL ON SCHEMA {APP_SCHEMA} FROM {API_ROLE}';
                    EXECUTE 'DROP ROLE {API_ROLE}';
                END IF;
            END
            $$
            """,
            # The schema itself stays: it holds alembic_version.
        ]
    )
