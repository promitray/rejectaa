"""Partner API clients (server-to-server integrations such as ChemLabFlow).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03

A client authenticates with OAuth2 client credentials: ``client_id`` (public)
plus a secret. Only the SHA-256 of each secret is stored, with a short
prefix and the last 4 characters so admins can tell secrets apart. A client
can hold two active secrets at once, for zero-downtime rotation.

Each client is on a plan; its usage is metered with subject_type='client'
(one pool for the whole partner). The plan archive guard now also counts
clients.
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, internal_function, run_sql, secure_table

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _updated_at_trigger(table: str) -> str:
    return (
        f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {S}.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION {S}.set_updated_at()"
    )


def upgrade() -> None:
    run_sql(
        [
            f"""
            CREATE TABLE {S}.api_clients (
                id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                client_id     text NOT NULL UNIQUE CHECK (client_id ~ '^rjc_[A-Za-z0-9_-]{{16,64}}$'),
                name          text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
                description   text NOT NULL DEFAULT '' CHECK (length(description) <= 500),
                plan_id       uuid NOT NULL REFERENCES {S}.plans (id) ON DELETE RESTRICT,
                scopes        text[] NOT NULL DEFAULT '{{}}',
                status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                token_version integer NOT NULL DEFAULT 0 CHECK (token_version >= 0),
                created_by    uuid REFERENCES {S}.users (id) ON DELETE SET NULL,
                last_used_at  timestamptz,
                created_at    timestamptz NOT NULL DEFAULT now(),
                updated_at    timestamptz NOT NULL DEFAULT now()
            )
            """,
            f"CREATE INDEX api_clients_plan_id_idx ON {S}.api_clients (plan_id)",
            _updated_at_trigger("api_clients"),
            # Disabling a client or changing its scopes/plan revokes its tokens.
            f"""
            CREATE FUNCTION {S}.api_clients_bump_version()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog
            AS $$
            BEGIN
                IF NEW.status IS DISTINCT FROM OLD.status
                   OR NEW.scopes IS DISTINCT FROM OLD.scopes
                   OR NEW.plan_id IS DISTINCT FROM OLD.plan_id THEN
                    NEW.token_version := OLD.token_version + 1;
                END IF;
                RETURN NEW;
            END
            $$
            """,
            *internal_function("api_clients_bump_version()"),
            f"""
            CREATE TRIGGER api_clients_bump_version BEFORE UPDATE ON {S}.api_clients
            FOR EACH ROW EXECUTE FUNCTION {S}.api_clients_bump_version()
            """,
            *secure_table("api_clients"),
            f"""
            CREATE TABLE {S}.api_client_secrets (
                id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                api_client_id uuid NOT NULL REFERENCES {S}.api_clients (id) ON DELETE CASCADE,
                secret_hash   bytea NOT NULL UNIQUE CHECK (octet_length(secret_hash) = 32),
                prefix        text NOT NULL CHECK (length(prefix) <= 16),
                last4         text NOT NULL CHECK (length(last4) = 4),
                created_by    uuid REFERENCES {S}.users (id) ON DELETE SET NULL,
                created_at    timestamptz NOT NULL DEFAULT now(),
                expires_at    timestamptz,
                revoked_at    timestamptz,
                last_used_at  timestamptz
            )
            """,
            f"CREATE INDEX api_client_secrets_client_idx ON {S}.api_client_secrets (api_client_id)",
            *secure_table("api_client_secrets"),
            # Plans used by clients cannot be archived either.
            f"""
            CREATE OR REPLACE FUNCTION {S}.plans_block_archive_with_users()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = {S}, pg_catalog
            AS $$
            BEGIN
                IF NEW.status = 'archived' AND OLD.status <> 'archived'
                   AND (EXISTS (SELECT 1 FROM users WHERE plan_id = NEW.id)
                        OR EXISTS (SELECT 1 FROM api_clients WHERE plan_id = NEW.id)) THEN
                    RAISE EXCEPTION 'Move users and partner clients to another plan before archiving this plan'
                        USING ERRCODE = 'P0001', CONSTRAINT = 'plans_block_archive_with_users';
                END IF;
                RETURN NEW;
            END
            $$
            """,
        ]
    )


def downgrade() -> None:
    run_sql(
        [
            f"""
            CREATE OR REPLACE FUNCTION {S}.plans_block_archive_with_users()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = {S}, pg_catalog
            AS $$
            BEGIN
                IF NEW.status = 'archived' AND OLD.status <> 'archived'
                   AND EXISTS (SELECT 1 FROM users WHERE plan_id = NEW.id) THEN
                    RAISE EXCEPTION 'Move users to another plan before archiving this plan'
                        USING ERRCODE = 'P0001', CONSTRAINT = 'plans_block_archive_with_users';
                END IF;
                RETURN NEW;
            END
            $$
            """,
            f"DROP TABLE IF EXISTS {S}.api_client_secrets",
            f"DROP TABLE IF EXISTS {S}.api_clients",
            f"DROP FUNCTION IF EXISTS {S}.api_clients_bump_version()",
        ]
    )
