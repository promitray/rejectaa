"""Shared building blocks for raw-SQL Alembic migrations.

Security model (enforced by these helpers):

* All Rejecta tables live in schema ``app``. Supabase's Data API only exposes
  ``public``/``graphql_public``, so ``app`` is never reachable over REST.
* Row Level Security is enabled on every table. The only policy is for the
  API role ``rejecta_api``; Supabase's ``anon``, ``authenticated`` and
  ``service_role`` get no grants at all.
* The API role is not the owner and has no BYPASSRLS, so it is fully
  subject to these rules. Migrations run as the owner (``postgres``).
"""

import textwrap
from collections.abc import Iterable

from alembic import context, op

APP_SCHEMA = "app"
API_ROLE = "rejecta_api"
SUPABASE_CLIENT_ROLES = ("anon", "authenticated", "service_role")

TABLE_RW = "SELECT, INSERT, UPDATE, DELETE"
TABLE_APPEND_ONLY = "SELECT, INSERT"


def run_sql(statements: Iterable[str]) -> None:
    """Execute raw SQL statements one by one.

    * One statement per call: asyncpg prepares each string, and a prepared
      statement cannot hold several commands.
    * Sent with ``exec_driver_sql`` instead of ``op.execute``: ``op.execute``
      wraps strings in ``text()``, which treats ``:word`` (for example the
      regex class ``[:space:]``) as a bind parameter and breaks the SQL.
    * In offline mode (``alembic upgrade head --sql``) the SQL is printed.
    """
    for statement in statements:
        sql = textwrap.dedent(statement).strip()
        if not sql:
            continue
        if context.is_offline_mode():
            context.get_context().impl.static_output(f"{sql};\n")
        else:
            op.get_bind().exec_driver_sql(sql)


def revoke_from_supabase_roles(object_sql: str) -> str:
    """REVOKE ALL on an object from Supabase client roles that exist.

    ``object_sql`` is the object clause, e.g. ``TABLE app.users`` or
    ``ALL FUNCTIONS IN SCHEMA app``. Roles are checked at run time so the
    same migration also works on a plain local Postgres.
    """
    roles = ", ".join(f"'{role}'" for role in SUPABASE_CLIENT_ROLES)
    return f"""
        DO $$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY[{roles}] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format('REVOKE ALL ON {object_sql} FROM %I', r);
                END IF;
            END LOOP;
        END
        $$
    """


def secure_table(table: str, privileges: str = TABLE_RW) -> list[str]:
    """Statements that lock a new ``app`` table down to the API role only."""
    qualified = f"{APP_SCHEMA}.{table}"
    return [
        f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY",
        f"REVOKE ALL ON TABLE {qualified} FROM PUBLIC",
        revoke_from_supabase_roles(f"TABLE {qualified}"),
        f"GRANT {privileges} ON TABLE {qualified} TO {API_ROLE}",
        (
            f"CREATE POLICY {table}_api_access ON {qualified} "
            f"AS PERMISSIVE FOR ALL TO {API_ROLE} USING (true) WITH CHECK (true)"
        ),
    ]


def secure_function(signature: str) -> list[str]:
    """Statements that let only the API role execute an ``app`` function."""
    qualified = f"{APP_SCHEMA}.{signature}"
    return [
        f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC",
        revoke_from_supabase_roles(f"FUNCTION {qualified}"),
        f"GRANT EXECUTE ON FUNCTION {qualified} TO {API_ROLE}",
    ]


def internal_function(signature: str) -> list[str]:
    """Statements for trigger-only functions: nobody may call them directly."""
    qualified = f"{APP_SCHEMA}.{signature}"
    return [
        f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC",
        revoke_from_supabase_roles(f"FUNCTION {qualified}"),
    ]
