"""Core tables: plans, features, users, sessions, usage, audit.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03

Integrity rules enforced by the database itself (not only by API code):

* At most one default plan (partial unique index) and, whenever plans exist,
  exactly one active default plan (deferred constraint trigger).
* A plan that still has users cannot be archived; it cannot be deleted
  either (FK ON DELETE RESTRICT).
* New users without a plan get the default plan.
* Changing a user's role, status, password or MFA bumps token_version, which
  invalidates every access and refresh token issued before the change.
* The last active admin cannot be demoted, suspended or deleted.
* Emails are stored trimmed and lower-case, unique.
"""

from collections.abc import Sequence

from db.migration_helpers import (
    APP_SCHEMA as S,
    TABLE_APPEND_ONLY,
    internal_function,
    run_sql,
    secure_table,
)

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES_IN_DROP_ORDER = (
    "audit_log",
    "usage_events",
    "usage_counters",
    "user_overrides",
    "password_reset_tokens",
    "mfa_recovery_codes",
    "refresh_tokens",
    "users",
    "plan_features",
    "features",
    "plans",
)


def _updated_at_trigger(table: str) -> str:
    return (
        f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {S}.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION {S}.set_updated_at()"
    )


PLANS = [
    f"""
    CREATE TABLE {S}.plans (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        key         text NOT NULL UNIQUE
                    CHECK (key ~ '^[a-z0-9][a-z0-9_-]{{1,48}}$'),
        name        text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
        description text NOT NULL DEFAULT '' CHECK (length(description) <= 500),
        is_default  boolean NOT NULL DEFAULT false,
        status      text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
        sort_order  integer NOT NULL DEFAULT 0,
        version     integer NOT NULL DEFAULT 1 CHECK (version >= 1),
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT plans_default_is_active CHECK (NOT is_default OR status = 'active')
    )
    """,
    f"CREATE UNIQUE INDEX plans_single_default ON {S}.plans (is_default) WHERE is_default",
    _updated_at_trigger("plans"),
    # Exactly one active default plan whenever any plan exists. Deferred, so
    # switching the default inside one transaction is allowed.
    f"""
    CREATE FUNCTION {S}.plans_require_default()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = {S}, pg_catalog
    AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM plans)
           AND NOT EXISTS (SELECT 1 FROM plans WHERE is_default AND status = 'active') THEN
            RAISE EXCEPTION 'Exactly one active default plan is required'
                USING ERRCODE = '23514', CONSTRAINT = 'plans_require_default';
        END IF;
        RETURN NULL;
    END
    $$
    """,
    *internal_function("plans_require_default()"),
    f"""
    CREATE CONSTRAINT TRIGGER plans_require_default
    AFTER INSERT OR UPDATE OR DELETE ON {S}.plans
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION {S}.plans_require_default()
    """,
    *secure_table("plans"),
]

FEATURES = [
    f"""
    CREATE TABLE {S}.features (
        key              text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9_]{{1,48}}$'),
        name             text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
        description      text NOT NULL DEFAULT '' CHECK (length(description) <= 500),
        globally_enabled boolean NOT NULL DEFAULT true,
        sort_order       integer NOT NULL DEFAULT 0,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now()
    )
    """,
    _updated_at_trigger("features"),
    *secure_table("features"),
]

# NULL limit = unlimited.
PLAN_FEATURES = [
    f"""
    CREATE TABLE {S}.plan_features (
        plan_id       uuid NOT NULL REFERENCES {S}.plans (id) ON DELETE CASCADE,
        feature_key   text NOT NULL REFERENCES {S}.features (key)
                      ON DELETE CASCADE ON UPDATE CASCADE,
        enabled       boolean NOT NULL DEFAULT false,
        daily_limit   integer CHECK (daily_limit >= 0),
        monthly_limit integer CHECK (monthly_limit >= 0),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (plan_id, feature_key),
        CONSTRAINT plan_features_daily_le_monthly
            CHECK (daily_limit IS NULL OR monthly_limit IS NULL OR daily_limit <= monthly_limit)
    )
    """,
    f"CREATE INDEX plan_features_feature_key_idx ON {S}.plan_features (feature_key)",
    _updated_at_trigger("plan_features"),
    *secure_table("plan_features"),
]

USERS = [
    f"""
    CREATE TABLE {S}.users (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        email               text NOT NULL,
        password_hash       text NOT NULL CHECK (length(password_hash) BETWEEN 20 AND 512),
        full_name           text NOT NULL DEFAULT '' CHECK (length(full_name) <= 120),
        role                text NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
        status              text NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'suspended')),
        plan_id             uuid NOT NULL REFERENCES {S}.plans (id) ON DELETE RESTRICT,
        token_version       integer NOT NULL DEFAULT 0 CHECK (token_version >= 0),
        failed_login_count  integer NOT NULL DEFAULT 0 CHECK (failed_login_count >= 0),
        locked_until        timestamptz,
        mfa_enabled         boolean NOT NULL DEFAULT false,
        mfa_secret_enc      text,
        mfa_last_step       bigint,
        last_login_at       timestamptz,
        password_changed_at timestamptz NOT NULL DEFAULT now(),
        created_at          timestamptz NOT NULL DEFAULT now(),
        updated_at          timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT users_email_normalized CHECK (
            email = lower(btrim(email))
            AND length(email) BETWEEN 3 AND 254
            AND email ~ '^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$'
        ),
        CONSTRAINT users_mfa_secret_present CHECK (NOT mfa_enabled OR mfa_secret_enc IS NOT NULL)
    )
    """,
    f"CREATE UNIQUE INDEX users_email_key ON {S}.users (email)",
    f"CREATE INDEX users_plan_id_idx ON {S}.users (plan_id)",
    f"CREATE INDEX users_role_status_idx ON {S}.users (role, status)",
    f"CREATE INDEX users_created_at_idx ON {S}.users (created_at DESC)",
    # Default plan on insert.
    f"""
    CREATE FUNCTION {S}.users_before_insert()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = {S}, pg_catalog
    AS $$
    BEGIN
        NEW.email := lower(btrim(NEW.email));
        IF NEW.plan_id IS NULL THEN
            SELECT id INTO NEW.plan_id FROM plans WHERE is_default AND status = 'active';
            IF NEW.plan_id IS NULL THEN
                RAISE EXCEPTION 'No default plan is configured' USING ERRCODE = 'P0001';
            END IF;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    *internal_function("users_before_insert()"),
    f"""
    CREATE TRIGGER users_before_insert BEFORE INSERT ON {S}.users
    FOR EACH ROW EXECUTE FUNCTION {S}.users_before_insert()
    """,
    # token_version bump + bookkeeping on update.
    f"""
    CREATE FUNCTION {S}.users_before_update()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = {S}, pg_catalog
    AS $$
    BEGIN
        NEW.updated_at := now();
        NEW.email := lower(btrim(NEW.email));
        IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
            NEW.password_changed_at := now();
        END IF;
        IF NEW.role IS DISTINCT FROM OLD.role
           OR NEW.status IS DISTINCT FROM OLD.status
           OR NEW.password_hash IS DISTINCT FROM OLD.password_hash
           OR NEW.mfa_enabled IS DISTINCT FROM OLD.mfa_enabled
           OR NEW.mfa_secret_enc IS DISTINCT FROM OLD.mfa_secret_enc THEN
            NEW.token_version := OLD.token_version + 1;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    *internal_function("users_before_update()"),
    f"""
    CREATE TRIGGER users_before_update BEFORE UPDATE ON {S}.users
    FOR EACH ROW EXECUTE FUNCTION {S}.users_before_update()
    """,
    # Last active admin guard. The advisory lock serialises concurrent
    # demotions so two admins cannot demote each other at the same time.
    f"""
    CREATE FUNCTION {S}.users_keep_one_admin()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = {S}, pg_catalog
    AS $$
    BEGIN
        IF OLD.role = 'admin' AND OLD.status = 'active'
           AND (TG_OP = 'DELETE' OR NEW.role <> 'admin' OR NEW.status <> 'active') THEN
            PERFORM pg_advisory_xact_lock(hashtext('app.users.last_admin'));
            IF NOT EXISTS (
                SELECT 1 FROM users
                WHERE role = 'admin' AND status = 'active' AND id <> OLD.id
            ) THEN
                RAISE EXCEPTION 'Cannot remove the last active admin'
                    USING ERRCODE = 'P0001', CONSTRAINT = 'users_keep_one_admin';
            END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    *internal_function("users_keep_one_admin()"),
    f"""
    CREATE TRIGGER users_keep_one_admin BEFORE UPDATE OR DELETE ON {S}.users
    FOR EACH ROW EXECUTE FUNCTION {S}.users_keep_one_admin()
    """,
    *secure_table("users"),
]

# Plan archive guard needs the users table, so it is created after it.
PLAN_ARCHIVE_GUARD = [
    f"""
    CREATE FUNCTION {S}.plans_block_archive_with_users()
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
    *internal_function("plans_block_archive_with_users()"),
    f"""
    CREATE TRIGGER plans_block_archive_with_users BEFORE UPDATE OF status ON {S}.plans
    FOR EACH ROW EXECUTE FUNCTION {S}.plans_block_archive_with_users()
    """,
]

# Refresh tokens: only the SHA-256 of the opaque token is stored.
REFRESH_TOKENS = [
    f"""
    CREATE TABLE {S}.refresh_tokens (
        id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id        uuid NOT NULL REFERENCES {S}.users (id) ON DELETE CASCADE,
        family_id      uuid NOT NULL,
        token_hash     bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
        token_version  integer NOT NULL,
        aal            smallint NOT NULL DEFAULT 1 CHECK (aal IN (1, 2)),
        created_at     timestamptz NOT NULL DEFAULT now(),
        expires_at     timestamptz NOT NULL,
        last_used_at   timestamptz,
        revoked_at     timestamptz,
        revoked_reason text CHECK (length(revoked_reason) <= 64),
        replaced_by    uuid REFERENCES {S}.refresh_tokens (id) ON DELETE SET NULL,
        ip             inet,
        user_agent     text CHECK (length(user_agent) <= 512),
        CONSTRAINT refresh_tokens_expiry_after_create CHECK (expires_at > created_at)
    )
    """,
    f"CREATE INDEX refresh_tokens_user_id_idx ON {S}.refresh_tokens (user_id)",
    f"CREATE INDEX refresh_tokens_family_id_idx ON {S}.refresh_tokens (family_id)",
    f"CREATE INDEX refresh_tokens_expires_at_idx ON {S}.refresh_tokens (expires_at)",
    *secure_table("refresh_tokens"),
]

MFA_RECOVERY_CODES = [
    f"""
    CREATE TABLE {S}.mfa_recovery_codes (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id    uuid NOT NULL REFERENCES {S}.users (id) ON DELETE CASCADE,
        code_hash  bytea NOT NULL CHECK (octet_length(code_hash) = 32),
        created_at timestamptz NOT NULL DEFAULT now(),
        used_at    timestamptz,
        UNIQUE (user_id, code_hash)
    )
    """,
    *secure_table("mfa_recovery_codes"),
]

PASSWORD_RESET_TOKENS = [
    f"""
    CREATE TABLE {S}.password_reset_tokens (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id    uuid NOT NULL REFERENCES {S}.users (id) ON DELETE CASCADE,
        token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
        created_by uuid REFERENCES {S}.users (id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        used_at    timestamptz,
        CONSTRAINT password_reset_expiry_after_create CHECK (expires_at > created_at)
    )
    """,
    f"CREATE INDEX password_reset_tokens_user_id_idx ON {S}.password_reset_tokens (user_id)",
    *secure_table("password_reset_tokens"),
]

# Per-user overrides. For each limit, *_set = false means "inherit from the
# plan"; *_set = true means "use this value", where NULL = unlimited.
USER_OVERRIDES = [
    f"""
    CREATE TABLE {S}.user_overrides (
        user_id           uuid NOT NULL REFERENCES {S}.users (id) ON DELETE CASCADE,
        feature_key       text NOT NULL REFERENCES {S}.features (key)
                          ON DELETE CASCADE ON UPDATE CASCADE,
        enabled           boolean,
        daily_limit_set   boolean NOT NULL DEFAULT false,
        daily_limit       integer CHECK (daily_limit >= 0),
        monthly_limit_set boolean NOT NULL DEFAULT false,
        monthly_limit     integer CHECK (monthly_limit >= 0),
        expires_at        timestamptz,
        reason            text NOT NULL DEFAULT '' CHECK (length(reason) <= 500),
        set_by            uuid REFERENCES {S}.users (id) ON DELETE SET NULL,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, feature_key),
        CONSTRAINT user_overrides_daily_value_needs_flag
            CHECK (daily_limit_set OR daily_limit IS NULL),
        CONSTRAINT user_overrides_monthly_value_needs_flag
            CHECK (monthly_limit_set OR monthly_limit IS NULL)
    )
    """,
    _updated_at_trigger("user_overrides"),
    *secure_table("user_overrides"),
]

# subject_type 'client' is reserved for partner API clients (later phase).
USAGE_COUNTERS = [
    f"""
    CREATE TABLE {S}.usage_counters (
        subject_type text NOT NULL CHECK (subject_type IN ('user', 'client')),
        subject_id   uuid NOT NULL,
        feature_key  text NOT NULL REFERENCES {S}.features (key)
                     ON DELETE CASCADE ON UPDATE CASCADE,
        period       text NOT NULL CHECK (period IN ('day', 'month')),
        period_start date NOT NULL,
        count        integer NOT NULL DEFAULT 0 CHECK (count >= 0),
        updated_at   timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (subject_type, subject_id, feature_key, period, period_start)
    )
    """,
    f"CREATE INDEX usage_counters_feature_period_idx ON {S}.usage_counters (feature_key, period, period_start)",
    *secure_table("usage_counters"),
]

USAGE_EVENTS = [
    f"""
    CREATE TABLE {S}.usage_events (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        subject_type text NOT NULL CHECK (subject_type IN ('user', 'client')),
        subject_id   uuid NOT NULL,
        feature_key  text NOT NULL,
        status       text NOT NULL DEFAULT 'reserved'
                     CHECK (status IN ('reserved', 'committed', 'refunded')),
        day_bucket   date NOT NULL,
        month_bucket date NOT NULL,
        request_id   text CHECK (length(request_id) <= 64),
        duration_ms  integer CHECK (duration_ms >= 0),
        created_at   timestamptz NOT NULL DEFAULT now(),
        finalized_at timestamptz
    )
    """,
    f"CREATE INDEX usage_events_subject_idx ON {S}.usage_events (subject_type, subject_id, created_at DESC)",
    f"CREATE INDEX usage_events_feature_idx ON {S}.usage_events (feature_key, created_at DESC)",
    f"CREATE INDEX usage_events_reserved_idx ON {S}.usage_events (created_at) WHERE status = 'reserved'",
    *secure_table("usage_events"),
]

# Append-only for the API role: it can INSERT and SELECT, never edit history.
AUDIT_LOG = [
    f"""
    CREATE TABLE {S}.audit_log (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        actor_type  text NOT NULL CHECK (actor_type IN ('user', 'system', 'client')),
        actor_id    uuid,
        action      text NOT NULL CHECK (length(action) BETWEEN 1 AND 80),
        target_type text CHECK (length(target_type) <= 40),
        target_id   text CHECK (length(target_id) <= 80),
        before      jsonb,
        after       jsonb,
        ip          inet,
        user_agent  text CHECK (length(user_agent) <= 512),
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX audit_log_created_at_idx ON {S}.audit_log (created_at DESC)",
    f"CREATE INDEX audit_log_actor_idx ON {S}.audit_log (actor_id, created_at DESC)",
    f"CREATE INDEX audit_log_target_idx ON {S}.audit_log (target_type, target_id, created_at DESC)",
    f"CREATE INDEX audit_log_action_idx ON {S}.audit_log (action, created_at DESC)",
    *secure_table("audit_log", privileges=TABLE_APPEND_ONLY),
]


def upgrade() -> None:
    run_sql(
        [
            *PLANS,
            *FEATURES,
            *PLAN_FEATURES,
            *USERS,
            *PLAN_ARCHIVE_GUARD,
            *REFRESH_TOKENS,
            *MFA_RECOVERY_CODES,
            *PASSWORD_RESET_TOKENS,
            *USER_OVERRIDES,
            *USAGE_COUNTERS,
            *USAGE_EVENTS,
            *AUDIT_LOG,
        ]
    )


def downgrade() -> None:
    run_sql(
        [
            *(f"DROP TABLE IF EXISTS {S}.{table} CASCADE" for table in TABLES_IN_DROP_ORDER),
            f"DROP FUNCTION IF EXISTS {S}.plans_require_default()",
            f"DROP FUNCTION IF EXISTS {S}.plans_block_archive_with_users()",
            f"DROP FUNCTION IF EXISTS {S}.users_before_insert()",
            f"DROP FUNCTION IF EXISTS {S}.users_before_update()",
            f"DROP FUNCTION IF EXISTS {S}.users_keep_one_admin()",
        ]
    )
