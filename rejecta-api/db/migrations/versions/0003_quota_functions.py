"""Atomic usage-quota functions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03

``consume_quota`` reserves one unit of a feature for a subject (user or
partner client) against a daily and a monthly limit in one statement per
bucket, using ``INSERT ... ON CONFLICT DO UPDATE ... WHERE count < limit``.
Postgres row locks make this race-free: N parallel calls against a limit
of L succeed exactly min(N, L) times. NULL limit = unlimited (still counted
so admins can see usage). Periods are UTC days and UTC calendar months.

``finalize_usage`` commits a reservation, or refunds it (the work failed),
decrementing the buckets the reservation was taken from, even if the day
has rolled over since.
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, run_sql, secure_function

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSUME_SIGNATURE = "consume_quota(text, uuid, text, integer, integer, text)"
FINALIZE_SIGNATURE = "finalize_usage(bigint, boolean, integer)"


def upgrade() -> None:
    run_sql(
        [
            f"""
            CREATE FUNCTION {S}.consume_quota(
                p_subject_type  text,
                p_subject_id    uuid,
                p_feature_key   text,
                p_daily_limit   integer,
                p_monthly_limit integer,
                p_request_id    text DEFAULT NULL
            )
            RETURNS TABLE (
                allowed         boolean,
                event_id        bigint,
                exceeded_period text,
                day_count       integer,
                month_count     integer
            )
            LANGUAGE plpgsql
            SET search_path = {S}, pg_catalog
            AS $$
            DECLARE
                v_now_utc     timestamp := timezone('UTC', now());
                v_day         date := v_now_utc::date;
                v_month       date := date_trunc('month', v_now_utc)::date;
                v_day_count   integer;
                v_month_count integer;
                v_event_id    bigint;
            BEGIN
                IF p_subject_type NOT IN ('user', 'client') THEN
                    RAISE EXCEPTION 'Invalid subject type %', p_subject_type USING ERRCODE = '22023';
                END IF;
                IF (p_daily_limit IS NOT NULL AND p_daily_limit < 0)
                   OR (p_monthly_limit IS NOT NULL AND p_monthly_limit < 0) THEN
                    RAISE EXCEPTION 'Limits cannot be negative' USING ERRCODE = '22023';
                END IF;

                -- Daily bucket. A limit of 0 inserts nothing (the SELECT ... WHERE
                -- yields no row), so v_day_count stays NULL and the call is denied.
                INSERT INTO usage_counters AS c
                    (subject_type, subject_id, feature_key, period, period_start, count)
                SELECT p_subject_type, p_subject_id, p_feature_key, 'day', v_day, 1
                WHERE p_daily_limit IS NULL OR p_daily_limit >= 1
                ON CONFLICT (subject_type, subject_id, feature_key, period, period_start)
                DO UPDATE SET count = c.count + 1, updated_at = now()
                    WHERE p_daily_limit IS NULL OR c.count < p_daily_limit
                RETURNING c.count INTO v_day_count;

                IF v_day_count IS NULL THEN
                    SELECT COALESCE(MAX(c.count) FILTER (WHERE c.period = 'day'), 0),
                           COALESCE(MAX(c.count) FILTER (WHERE c.period = 'month'), 0)
                      INTO v_day_count, v_month_count
                      FROM usage_counters c
                     WHERE c.subject_type = p_subject_type AND c.subject_id = p_subject_id
                       AND c.feature_key = p_feature_key
                       AND ((c.period = 'day' AND c.period_start = v_day)
                            OR (c.period = 'month' AND c.period_start = v_month));
                    RETURN QUERY SELECT false, NULL::bigint, 'day'::text, v_day_count, v_month_count;
                    RETURN;
                END IF;

                -- Monthly bucket.
                INSERT INTO usage_counters AS c
                    (subject_type, subject_id, feature_key, period, period_start, count)
                SELECT p_subject_type, p_subject_id, p_feature_key, 'month', v_month, 1
                WHERE p_monthly_limit IS NULL OR p_monthly_limit >= 1
                ON CONFLICT (subject_type, subject_id, feature_key, period, period_start)
                DO UPDATE SET count = c.count + 1, updated_at = now()
                    WHERE p_monthly_limit IS NULL OR c.count < p_monthly_limit
                RETURNING c.count INTO v_month_count;

                IF v_month_count IS NULL THEN
                    -- Undo the daily increment taken above (row is still locked by us).
                    UPDATE usage_counters c
                       SET count = c.count - 1, updated_at = now()
                     WHERE c.subject_type = p_subject_type AND c.subject_id = p_subject_id
                       AND c.feature_key = p_feature_key AND c.period = 'day'
                       AND c.period_start = v_day
                    RETURNING c.count INTO v_day_count;
                    SELECT COALESCE(MAX(c.count), 0) INTO v_month_count
                      FROM usage_counters c
                     WHERE c.subject_type = p_subject_type AND c.subject_id = p_subject_id
                       AND c.feature_key = p_feature_key AND c.period = 'month'
                       AND c.period_start = v_month;
                    RETURN QUERY SELECT false, NULL::bigint, 'month'::text, v_day_count, v_month_count;
                    RETURN;
                END IF;

                INSERT INTO usage_events
                    (subject_type, subject_id, feature_key, status, day_bucket, month_bucket, request_id)
                VALUES
                    (p_subject_type, p_subject_id, p_feature_key, 'reserved', v_day, v_month, p_request_id)
                RETURNING id INTO v_event_id;

                RETURN QUERY SELECT true, v_event_id, NULL::text, v_day_count, v_month_count;
            END
            $$
            """,
            *secure_function(CONSUME_SIGNATURE),
            f"""
            CREATE FUNCTION {S}.finalize_usage(
                p_event_id    bigint,
                p_success     boolean,
                p_duration_ms integer DEFAULT NULL
            )
            RETURNS text
            LANGUAGE plpgsql
            SET search_path = {S}, pg_catalog
            AS $$
            DECLARE
                v_event usage_events%ROWTYPE;
            BEGIN
                UPDATE usage_events e
                   SET status = CASE WHEN p_success THEN 'committed' ELSE 'refunded' END,
                       finalized_at = now(),
                       duration_ms = p_duration_ms
                 WHERE e.id = p_event_id AND e.status = 'reserved'
                RETURNING e.* INTO v_event;

                IF NOT FOUND THEN
                    RETURN 'noop';
                END IF;

                IF NOT p_success THEN
                    UPDATE usage_counters c
                       SET count = GREATEST(c.count - 1, 0), updated_at = now()
                     WHERE c.subject_type = v_event.subject_type
                       AND c.subject_id = v_event.subject_id
                       AND c.feature_key = v_event.feature_key
                       AND ((c.period = 'day' AND c.period_start = v_event.day_bucket)
                            OR (c.period = 'month' AND c.period_start = v_event.month_bucket));
                END IF;

                RETURN v_event.status;
            END
            $$
            """,
            *secure_function(FINALIZE_SIGNATURE),
        ]
    )


def downgrade() -> None:
    run_sql(
        [
            f"DROP FUNCTION IF EXISTS {S}.{FINALIZE_SIGNATURE}",
            f"DROP FUNCTION IF EXISTS {S}.{CONSUME_SIGNATURE}",
        ]
    )
