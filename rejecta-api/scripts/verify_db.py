"""Verify the database security model and integrity rules end to end.

    python -m scripts.verify_db

Security checks run on the catalog as the owner. Behaviour checks run as
the API role inside transactions that are rolled back, so no test data is
left behind. The concurrency check commits rows for a random subject id and
deletes them afterwards. Exit code is 1 if any check fails.
"""

import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable

import asyncpg

from db.migration_helpers import API_ROLE, APP_SCHEMA, SUPABASE_CLIENT_ROLES
from scripts._common import connect_api, connect_owner

FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$" + "x" * 40
EXPECTED_FEATURES = {"analyse_paper", "voice_calibrate", "abstract_forge", "rebuttal_writer", "ai_heatmap"}
EXPECTED_PLANS = {"free", "researcher", "lab", "partner"}
CONCURRENT_CALLS = 20
CONCURRENT_LIMIT = 3

results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((condition, label))
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")


async def expect_error(conn: asyncpg.Connection, sql: str, *args: object, contains: str = "") -> bool:
    """True if the statement fails (inside a savepoint) with the expected message."""
    try:
        async with conn.transaction():
            await conn.execute(sql, *args)
    except asyncpg.PostgresError as exc:
        return contains.lower() in str(exc).lower()
    return False


async def in_rollback(conn: asyncpg.Connection, body: Callable[[], Awaitable[None]]) -> None:
    tx = conn.transaction()
    await tx.start()
    try:
        await body()
    finally:
        await tx.rollback()


async def security_checks() -> None:
    print("Security (catalog, as owner)")
    owner = await connect_owner()
    try:
        tables = await owner.fetch(
            """
            SELECT c.relname, c.relrowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relkind = 'r' AND c.relname <> 'alembic_version'
            """,
            APP_SCHEMA,
        )
        check(len(tables) == 14, f"14 app tables exist (found {len(tables)})")
        no_rls = [t["relname"] for t in tables if not t["relrowsecurity"]]
        check(not no_rls, f"RLS enabled on every table {no_rls or ''}")

        for role in SUPABASE_CLIENT_ROLES:
            if not await owner.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                continue
            usage = await owner.fetchval("SELECT has_schema_privilege($1, $2, 'USAGE')", role, APP_SCHEMA)
            check(not usage, f"{role}: no USAGE on schema app")
            leaked = await owner.fetch(
                """
                SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $2 AND c.relkind = 'r'
                  AND has_table_privilege($1, c.oid, 'SELECT,INSERT,UPDATE,DELETE')
                """,
                role,
                APP_SCHEMA,
            )
            check(not leaked, f"{role}: no table privileges {[r['relname'] for r in leaked] or ''}")
            funcs = await owner.fetch(
                """
                SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = $2 AND has_function_privilege($1, p.oid, 'EXECUTE')
                """,
                role,
                APP_SCHEMA,
            )
            check(not funcs, f"{role}: cannot execute app functions {[f['proname'] for f in funcs] or ''}")

        attrs = await owner.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcanlogin FROM pg_roles WHERE rolname = $1",
            API_ROLE,
        )
        check(attrs is not None and not attrs["rolsuper"] and not attrs["rolbypassrls"]
              and not attrs["rolcreaterole"], f"{API_ROLE}: no superuser / BYPASSRLS / CREATEROLE")
        check(attrs is not None and attrs["rolcanlogin"], f"{API_ROLE}: can log in")
        owns = await owner.fetchval(
            """
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND pg_get_userbyid(c.relowner) = $2
            """,
            APP_SCHEMA,
            API_ROLE,
        )
        check(owns == 0, f"{API_ROLE}: owns no tables (so RLS applies to it)")
        for privilege in ("UPDATE", "DELETE"):
            allowed = await owner.fetchval(
                f"SELECT has_table_privilege($1, '{APP_SCHEMA}.audit_log', '{privilege}')", API_ROLE
            )
            check(not allowed, f"{API_ROLE}: cannot {privilege} audit_log (append-only)")
        writes_version = await owner.fetchval(
            f"SELECT has_table_privilege($1, '{APP_SCHEMA}.alembic_version', 'INSERT,UPDATE,DELETE')",
            API_ROLE,
        )
        check(not writes_version, f"{API_ROLE}: cannot modify alembic_version")
    finally:
        await owner.close()


async def behaviour_checks() -> None:
    print("Integrity rules (as rejecta_api, rolled back)")
    conn = await connect_api()
    try:
        role = await conn.fetchval("SELECT current_user")
        check(role == API_ROLE, f"connected as {API_ROLE}")

        features = {r["key"] for r in await conn.fetch("SELECT key FROM features")}
        check(features == EXPECTED_FEATURES, "5 seeded features")
        plans = {r["key"]: r["is_default"] for r in await conn.fetch("SELECT key, is_default FROM plans")}
        check(set(plans) == EXPECTED_PLANS, "4 seeded plans")
        check([k for k, d in plans.items() if d] == ["free"], "exactly one default plan: free")
        matrix = await conn.fetchval("SELECT count(*) FROM plan_features")
        check(matrix == 20, f"plan x feature matrix complete (20 rows, found {matrix})")

        async def users_rules() -> None:
            user = await conn.fetchrow(
                "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING *",
                "  Test.User@Example.COM ",
                FAKE_HASH,
            )
            free_id = await conn.fetchval("SELECT id FROM plans WHERE key = 'free'")
            check(user["plan_id"] == free_id, "new user gets the default plan")
            check(user["email"] == "test.user@example.com", "email stored trimmed + lower-case")
            check(user["role"] == "user" and user["status"] == "active", "new user is role=user, active")
            check(
                await expect_error(conn, "INSERT INTO users (email, password_hash) VALUES ($1, $2)",
                                   "TEST.user@example.com", FAKE_HASH, contains="users_email_key"),
                "duplicate email (any case) rejected",
            )
            tv = await conn.fetchval(
                "UPDATE users SET password_hash = $2 WHERE id = $1 RETURNING token_version",
                user["id"], FAKE_HASH + "y",
            )
            check(tv == user["token_version"] + 1, "password change bumps token_version")
            tv2 = await conn.fetchval(
                "UPDATE users SET full_name = 'X' WHERE id = $1 RETURNING token_version", user["id"]
            )
            check(tv2 == tv, "unrelated update keeps token_version")

            admin_id = await conn.fetchval(
                "INSERT INTO users (email, password_hash, role) VALUES ('a1@example.com', $1, 'admin') RETURNING id",
                FAKE_HASH,
            )
            check(
                await expect_error(conn, "UPDATE users SET role = 'user' WHERE id = $1", admin_id,
                                   contains="last active admin"),
                "last active admin cannot be demoted",
            )
            check(
                await expect_error(conn, "UPDATE users SET status = 'suspended' WHERE id = $1", admin_id,
                                   contains="last active admin"),
                "last active admin cannot be suspended",
            )
            check(
                await expect_error(conn, "DELETE FROM users WHERE id = $1", admin_id,
                                   contains="last active admin"),
                "last active admin cannot be deleted",
            )
            await conn.execute(
                "INSERT INTO users (email, password_hash, role) VALUES ('a2@example.com', $1, 'admin')", FAKE_HASH
            )
            await conn.execute("UPDATE users SET role = 'user' WHERE id = $1", admin_id)
            check(True, "an admin can be demoted while another active admin exists")

            await conn.execute(
                "UPDATE users SET plan_id = (SELECT id FROM plans WHERE key = 'researcher') WHERE id = $1",
                user["id"],
            )
            check(
                await expect_error(conn, "UPDATE plans SET status = 'archived' WHERE key = 'researcher'",
                                   contains="before archiving"),
                "plan with users cannot be archived",
            )
            check(
                await expect_error(conn, "DELETE FROM plans WHERE key = 'researcher'", contains="foreign key"),
                "plan with users cannot be deleted",
            )
            check(
                await expect_error(conn, "UPDATE plans SET is_default = true WHERE key = 'lab'",
                                   contains="plans_single_default"),
                "second default plan rejected",
            )
            check(
                await expect_error(conn, "UPDATE plan_features SET daily_limit = 10, monthly_limit = 5 "
                                         "WHERE feature_key = 'analyse_paper'",
                                   contains="daily_le_monthly"),
                "daily limit above monthly limit rejected",
            )
            check(
                await expect_error(conn, "UPDATE plan_features SET daily_limit = -1", contains="check"),
                "negative limit rejected",
            )
            check(
                await expect_error(conn, "UPDATE audit_log SET action = 'x'", contains="permission denied"),
                "audit_log rows cannot be edited by the API",
            )

        await in_rollback(conn, users_rules)

        async def default_plan_rule() -> None:
            await conn.execute("UPDATE plans SET is_default = false WHERE key = 'free'")
            check(
                await expect_error(conn, "SET CONSTRAINTS ALL IMMEDIATE", contains="default plan"),
                "removing the only default plan rejected at commit",
            )

        await in_rollback(conn, default_plan_rule)

        async def default_switch_rule() -> None:
            await conn.execute("UPDATE plans SET is_default = false WHERE key = 'free'")
            await conn.execute("UPDATE plans SET is_default = true WHERE key = 'researcher'")
            await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            check(True, "switching the default plan in one transaction allowed")

        await in_rollback(conn, default_switch_rule)

        async def quota_rules() -> None:
            subject = uuid.uuid4()
            consume = "SELECT * FROM consume_quota('user', $1, $2, $3, $4)"
            rows = [await conn.fetchrow(consume, subject, "analyse_paper", 3, None) for _ in range(4)]
            check([r["allowed"] for r in rows] == [True, True, True, False], "daily limit 3: 3 allowed, 4th denied")
            check(rows[3]["exceeded_period"] == "day" and rows[3]["day_count"] == 3, "denial reports period=day, count=3")

            status = await conn.fetchval("SELECT finalize_usage($1, false, 120)", rows[0]["event_id"])
            check(status == "refunded", "failed work is refunded")
            again = await conn.fetchrow(consume, subject, "analyse_paper", 3, None)
            check(again["allowed"] and again["day_count"] == 3, "refunded credit can be used again")
            check(await conn.fetchval("SELECT finalize_usage($1, false)", rows[0]["event_id"]) == "noop",
                  "double refund is a no-op")
            check(await conn.fetchval("SELECT finalize_usage($1, true, 50)", rows[1]["event_id"]) == "committed",
                  "successful work is committed")

            monthly = [await conn.fetchrow(consume, subject, "voice_calibrate", None, 2) for _ in range(3)]
            check([r["allowed"] for r in monthly] == [True, True, False], "monthly limit 2: 3rd denied")
            check(monthly[2]["exceeded_period"] == "month" and monthly[2]["day_count"] == 2,
                  "monthly denial does not consume the daily bucket")

            zero = await conn.fetchrow(consume, subject, "rebuttal_writer", 0, None)
            check(not zero["allowed"] and zero["exceeded_period"] == "day", "limit 0 denies immediately")
            unlimited = [await conn.fetchrow(consume, subject, "ai_heatmap", None, None) for _ in range(5)]
            check(all(r["allowed"] for r in unlimited) and unlimited[-1]["day_count"] == 5,
                  "unlimited is allowed and still counted")

        await in_rollback(conn, quota_rules)
    finally:
        await conn.close()


async def concurrency_check() -> None:
    print(f"Concurrency ({CONCURRENT_CALLS} parallel calls, limit {CONCURRENT_LIMIT}, committed then cleaned)")
    subject = uuid.uuid4()
    conns = [await connect_api() for _ in range(CONCURRENT_CALLS)]
    try:
        rows = await asyncio.gather(
            *(
                c.fetchrow("SELECT * FROM consume_quota('user', $1, 'analyse_paper', $2, NULL)", subject, CONCURRENT_LIMIT)
                for c in conns
            )
        )
        allowed = sum(1 for r in rows if r["allowed"])
        check(allowed == CONCURRENT_LIMIT, f"exactly {CONCURRENT_LIMIT} of {CONCURRENT_CALLS} allowed (got {allowed})")
    finally:
        await conns[0].execute("DELETE FROM usage_events WHERE subject_id = $1", subject)
        await conns[0].execute("DELETE FROM usage_counters WHERE subject_id = $1", subject)
        await asyncio.gather(*(c.close() for c in conns))


async def main() -> int:
    await security_checks()
    await behaviour_checks()
    await concurrency_check()
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
