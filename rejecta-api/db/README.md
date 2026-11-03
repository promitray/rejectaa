# Database (Supabase Postgres)

Supabase is used **only as Postgres**. Supabase Auth, the anon key and the
REST/Data API are not used. All Rejecta tables live in schema `app`, which the
Data API does not expose.

## Two connections

| Variable | Role | Used by |
|---|---|---|
| `MIGRATION_DATABASE_URL` | `postgres` (schema owner) | Alembic and `scripts/*` only |
| `DATABASE_URL` | `rejecta_api` (least privilege) | The running API |

`rejecta_api` is not the owner of any table, has no `BYPASSRLS`, cannot edit
`audit_log` (append-only) or `alembic_version`, and has session limits
(`statement_timeout` 15s, `idle_in_transaction_session_timeout` 30s).
Supabase's `anon`, `authenticated` and `service_role` have no access to `app`.

Passwords containing `@ / # :` are fine in either URL; they are encoded
automatically. A literal `%` must be written as `%25`.

## Setup / deploy

Run from `rejecta-api/`:

```bash
alembic upgrade head                       # apply migrations (as postgres)
python -m scripts.db_role --write-env      # set rejecta_api password, write DATABASE_URL
python -m scripts.verify_db                # 51 security + integrity checks
```

`scripts.db_role` without `--write-env` prints the URL instead (for Railway).
Re-running it rotates the password.

Railway has no outbound IPv6, so on Railway use Supabase's **Session pooler**
host (`aws-0-<region>.pooler.supabase.com:5432`, user `postgres.<project-ref>`).
`scripts.db_role` then builds `rejecta_api.<project-ref>` automatically.

## Writing a migration

```bash
alembic revision -m "short description"
```

Migrations are raw SQL through `db.migration_helpers.run_sql([...])`, one
statement per string. Every new table **must** include
`*secure_table("table_name")` (RLS + API-role policy + no Supabase role
access). Every new function callable by the API must include
`*secure_function("name(arg types)")`; trigger-only functions use
`*internal_function(...)`. Run `python -m scripts.verify_db` afterwards.

## Rules enforced by the database

- Exactly one active default plan whenever plans exist; switching the
  default inside one transaction is allowed.
- A plan with users cannot be archived or deleted.
- New users without `plan_id` get the default plan.
- Changing a user's role, status, password or MFA bumps `token_version`
  (all earlier tokens become invalid).
- The last active admin cannot be demoted, suspended or deleted.
- Emails are stored trimmed + lower-case and are unique.
- `consume_quota` / `finalize_usage`: race-free daily + monthly quotas (UTC),
  with refunds when the work fails.
