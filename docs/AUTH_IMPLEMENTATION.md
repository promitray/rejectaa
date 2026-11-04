# Rejecta authentication & access control

> A narrative report with diagrams (architecture, flows, decisions, security) is in [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md). This file is the engineering log.

Living implementation log for role-based authentication in Rejecta.
Updated at the end of every phase.

- **Branch:** `feat/auth-system`
- **Last updated:** 3 Nov 2026, all 9 phases complete (not yet deployed)

---

## 1. What we are building

Role-based authentication for Rejecta with two roles:

| Role | Can do |
|---|---|
| `admin` | Everything, plus manage users, plans, usage limits, which features each plan gets, and partner integrations. |
| `user` | Use the tools their plan allows, within its limits. |

Requirements that shaped the design:

- **Our own JWT auth.** Supabase is used only as the Postgres database. Supabase Auth, the anon key and the REST API are not used.
- **Integration-ready.** Partner platforms such as ChemLabFlow will call the API server-to-server.
- **Admin-managed plans.** Admins manage plans, limits and features from a plan management page, wired end to end to the backend.
- **No email for now.** There is no email verification and no email-based password reset until an email provider is chosen.
- **2FA for admins only.** Admins must use TOTP. Users and partner integrations are not affected.

## 2. Architecture

```text
 Browser (rejecta.scivalon.com, Netlify)
   │
   ├── /api/auth/*  ──▶ Netlify proxy (same origin) ──▶ Railway  /auth/*
   │                     refresh cookie is first-party + HttpOnly
   │
   └── all other calls ── direct ──▶ Railway (Authorization: Bearer <access JWT>)
                                       │
                 FastAPI: verify JWT → load user (status, role, token_version)
                          → permission check → feature/quota check → work
                                       │
                                       ▼
                          Supabase Postgres, schema "app"
```

- **Why auth goes through Netlify:** the API (`*.railway.app`) and the app (`scivalon.com`) are different sites. A cookie set by Railway would be a third-party cookie, and Safari blocks those. Through the Netlify proxy the cookie belongs to the app's own domain.
- **Why the heavy calls go direct:** analysis calls can take 15 to 45 seconds. Sending them direct avoids proxy timeouts.

## 3. Phase status

| # | Phase | Status |
|---|---|---|
| 1 | Database foundation: schema, RLS lockdown, quotas, seed data | ✅ Done, applied to Supabase |
| 2 | Auth core: passwords, JWT, refresh sessions, `/me` | ✅ Done, applied to Supabase |
| 3 | Admin 2FA (TOTP) and recovery codes | ✅ Done, applied to Supabase |
| 4 | Feature gating and quotas on the tool endpoints, CORS lockdown | ✅ Done |
| 5 | Admin API: users, plans, features, overrides, usage, audit, reset links | ✅ Done |
| 6 | Frontend auth: login/signup pages, guards, token handling, proxy config | ✅ Done |
| 7 | Frontend admin: dashboard, users, **plan management**, usage, audit log | ✅ Done |
| 8 | Partner integrations (ChemLabFlow): client credentials | ✅ Done, applied to Supabase |
| 9 | Hardening, production checklist | ✅ Done (deploy steps in §12) |

**Database:** migrations `0001`–`0007` are applied to the Supabase project. **Code:** the backend and the frontend are on branch `feat/auth-system`, not yet deployed. The production site keeps working unchanged until you deploy using §12.

---

## 4. Phase 1: database foundation ✅

Migrations are managed by **Alembic in raw-SQL mode**: no ORM models, one SQL statement per call. They live in [`rejecta-api/db/migrations/versions`](../rejecta-api/db/migrations/versions). Details are in [`rejecta-api/db/README.md`](../rejecta-api/db/README.md).

| Migration | What it does |
|---|---|
| `0001` | Creates schema `app` and the role `rejecta_api`. Removes all access to the schema for Supabase's `anon`, `authenticated` and `service_role`. |
| `0002` | Creates 11 tables with constraints and integrity triggers. |
| `0003` | Adds `consume_quota` and `finalize_usage`: atomic daily and monthly quotas, with refunds. |
| `0004` | Seeds 5 features and 4 plans (Free is the default, plus Researcher, Lab and Partner). |
| `0005` | Adds an absolute session lifetime (`session_started_at`). |
| `0006` | Adds a pending TOTP secret column for admin MFA enrollment (Phase 3). |
| `0007` | Adds the partner tables `api_clients` and `api_client_secrets`; archive guard counts partners too (Phase 8). |

### Tables

`plans`, `features`, `plan_features`, `users`, `refresh_tokens`, `mfa_recovery_codes`, `password_reset_tokens`, `user_overrides`, `usage_counters`, `usage_events`, `audit_log`.

### Security model

- **Two connections:**
  - The API runs as `rejecta_api`. It owns no tables, has no `BYPASSRLS`, and has statement and idle timeouts.
  - Migrations run as `postgres`.
- **RLS:** Row Level Security is on for every table. The only policy is for `rejecta_api`.
- **Schema exposure:** schema `app` is not exposed by Supabase's Data API.
- **Append-only history:** `audit_log` accepts INSERT and SELECT only. `alembic_version` is read-only for the API.
- **No plain password on the server:** the `rejecta_api` password is sent to Postgres as a SCRAM verifier.

### Rules the database enforces itself

- **One default plan:** exactly one active default plan exists, and switching the default inside one transaction is allowed.
- **Plans in use stay:** a plan with users cannot be archived or deleted.
- **Default plan for new users:** new users get the default plan automatically.
- **Token invalidation:** changing a user's role, status, password or MFA bumps `token_version`, which kills every earlier token.
- **Last admin protected:** the last active admin cannot be demoted, suspended or deleted. Concurrent changes are safe.
- **Emails:** stored trimmed and lower-case, and unique.
- **Quotas:** race-free. In testing, 20 parallel calls against a limit of 3 allowed exactly 3.

**Verification:** `python -m scripts.verify_db` runs 51 security and integrity checks. All of them pass on Supabase.

### Starter plans (admins will edit these in Phase 7)

| Feature | Free (default) | Researcher | Lab | Partner |
|---|---|---|---|---|
| Paper analysis | 3/day | 20/month | ∞ | ∞ |
| Voice Calibrator | 5/day | 100/month | ∞ | ∞ |
| Abstract Forge | 5/day | 100/month | ∞ | ∞ |
| Rebuttal Writer | 3/day | 50/month | ∞ | ∞ |
| AI Signal Heatmap | 20/day | ∞ | ∞ | ∞ |

---

## 5. Phase 2: auth core ✅

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/signup` | cookie route¹ | Create an account (role `user`, default plan) and sign in. |
| POST | `/auth/login` | cookie route¹ | Sign in with email and password. |
| POST | `/auth/refresh` | cookie route¹ | Rotate the refresh cookie and get a new access token. |
| POST | `/auth/logout` | cookie route¹ | End this browser's session. |
| POST | `/auth/logout-all` | Bearer | End every session and invalidate all access tokens. |
| POST | `/auth/change-password` | Bearer | Change the password. Other devices are signed out; this device gets a new session. |
| GET | `/me` | Bearer | Profile, role, plan and auth level. |
| GET | `/.well-known/jwks.json` | public | Public signing keys. |

¹ Cookie routes need the header `X-Rejecta-Client: web`. If the browser sends an `Origin`, it must be one of the app origins. This is the CSRF guard.

**Response from signup, login, refresh and change-password:**

```json
{
  "access_token": "<JWT>",
  "token_type": "Bearer",
  "expires_in": 900,
  "user": { "id": "…", "email": "…", "full_name": "…", "role": "user", "created_at": "…" }
}
```

The response also sets the refresh token as a cookie: `Set-Cookie: rejecta_rt=…; HttpOnly; Secure; SameSite=Strict; Path=/api/auth`.

**Errors** always look like `{"detail": "<message>", "code": "<code>"}`:

| Code | HTTP | When |
|---|---|---|
| `invalid_credentials` | 401 | Wrong email or password. The message is identical for unknown emails. |
| `missing_token` / `invalid_token` | 401 | No access token, or a bad, expired or revoked one. |
| `missing_refresh_token` / `invalid_refresh_token` | 401 | The session is gone. The client should show the login page. |
| `refresh_superseded` | 401 | Another tab just refreshed. The client should retry once. |
| `email_taken` | 409 | Sign-up with an email that already has an account. |
| `weak_password` | 400 | The password breaks the policy. |
| `invalid_current_password` | 400 | Change-password was given the wrong current password. |
| `account_suspended` | 403 | The password was correct, but the account is suspended. |
| `csrf_check_failed` | 403 | The `X-Rejecta-Client` header is missing, or the origin is not allowed. |
| `invalid_mfa_token` | 401 | The admin's 5-minute MFA token is expired, already used, or void because the password or role changed. Sign in again. |
| `invalid_mfa_code` | 401 | Wrong, reused or expired TOTP code, or an unknown or used recovery code. |
| `mfa_enrollment_not_started` / `mfa_already_enabled` / `mfa_state_conflict` | 409 | An enrollment step was called out of order. |
| `admin_required` | 403 | A non-admin called an admin route. |
| `admin_mfa_required` | 403 | An admin session without two-factor (aal=1) called an admin route. |
| `rate_limited` | 429 | Too many attempts. The response includes a `Retry-After` header. |
| `auth_unavailable` | 503 | Signing keys or the database are not configured. |

### Access token (JWT)

- **Signing:** Ed25519 (`EdDSA`). The private key exists only in the environment (`JWT_PRIVATE_KEY`), and `kid` is in the header.
- **Lifetime:** 15 minutes. The frontend keeps it in memory only.
- **Claims:** `iss`, `aud`, `sub`, `typ=access`, `role`, `tv` (token_version), `sid` (session id), `aal` (1 = password, 2 = password + TOTP), `iat`, `nbf`, `exp`, `jti`.
- **Verification rejects:**
  - any algorithm other than EdDSA, including `none` and HS256 key confusion
  - an unknown `kid`
  - a wrong issuer or audience
  - expired or not-yet-valid tokens
  - a wrong `typ`
  - malformed or missing claims
- **Every request re-checks the database:** the user must still exist, be active, and have the same `token_version`. The DB lookup is cached for 30 seconds and cleared on changes made through the API.
- **Key rotation:** run `python -m scripts.generate_keys --write-env --rotate`. The old public key stays valid for 15 minutes.

### Refresh sessions

- **Format:** the refresh token is opaque (`rt_` plus 256 random bits). Only its SHA-256 hash is stored.
- **Rotation:** every refresh replaces the token.
- **Reuse detection:** presenting an already-rotated token revokes the whole session and writes `auth.refresh_reuse_detected` to the audit log.
- **Two tabs at once:** if two tabs refresh within 10 seconds, the second gets `refresh_superseded`. Nothing is revoked and no tokens are issued.
- **Lifetime:** 30 days sliding, and never more than 90 days after login.
- **Revocation:** a session dies on logout, logout-all, password change, suspension or role change, because each of these changes `token_version`.

### Passwords and login protection

- **Hashing:** Argon2id (argon2-cffi defaults). It runs in worker threads, at most 4 at a time, to keep memory bounded.
- **Policy:** 10 to 128 characters. Common passwords, very repetitive passwords and the user's own email are rejected. There are no forced symbol rules, following NIST guidance.
- **Account enumeration:**
  - Login answers unknown emails with the same message, and in about the same time (a dummy hash is verified).
  - Sign-up does reveal that an email is taken; without email verification this cannot be avoided. Sign-up is rate-limited to 5 per IP per hour to limit this.

**Rate limits and lockout:**

| What | Limit |
|---|---|
| Login attempts per IP | 20 per 5 minutes |
| Failed logins per email | 5 per 15 minutes |
| Account lockout (in the DB, survives restarts) | after 5 failures, for 15 minutes |
| Refresh per IP | 60 per minute |
| Password change per user | 5 per 15 minutes |

- **Client IP:** the right-most `X-Forwarded-For` entry, which is the address Railway saw. Optionally, the Netlify signed-proxy IP, checked with `NETLIFY_PROXY_SECRET`.
- **Mass assignment:** request bodies reject unknown fields. Sending `role` or `plan_id` at sign-up returns 422.
- **Audit log:** sign-up, login, failed login, logout, logout-all, password change and refresh-token reuse are all recorded.

### Scripts

| Command | Purpose |
|---|---|
| `python -m scripts.generate_keys --write-env` | Create the JWT signing key and `MFA_ENCRYPTION_KEY` if missing (`--rotate` replaces the JWT key). |
| `python -m scripts.create_admin --email you@x.com --name "You"` | Create the first admin, with a hidden password prompt. Admins can never be created through the API. |
| `python -m scripts.create_admin --email you@x.com --promote` | Make an existing user an admin. |
| `python -m scripts.create_admin --email you@x.com --reset-mfa` | Break-glass: remove an admin's 2FA (Phase 3). |
| `python -m scripts.db_role --write-env` | Set or rotate the `rejecta_api` DB password. |
| `python -m scripts.verify_db` | Run the 51 database checks. |

### Tests

| Suite | Count | What it covers |
|---|---|---|
| Unit: tokens | 22 | Roundtrip, expiry, `nbf`, issuer/audience, `alg=none`, HS256 confusion, unknown and foreign keys, tampering, bad claims, key rotation, JWKS. |
| Unit: passwords | 12 | Argon2id roundtrip, salts, bad hashes, policy. |
| Unit: rate limit and IP | 10 | Sliding window, memory bound, X-Forwarded-For spoofing, Netlify signature. |
| Integration | 24 | Every endpoint end to end on a real Postgres 17 (Docker): rotation and reuse detection, lockout, suspension, logout-all, password change, CSRF, mass assignment, rate limits, audit trail. |

At the end of Phase 2 the full suite had 99 passed and 3 skipped. The 3 skipped are older async tests for the calibrator, forge and rebuttal tools; they need an async pytest plugin and are not related to auth.

---

## 6. Phase 3: admin two-factor authentication ✅

A password alone never gives an admin a session. Regular users are not affected: they log in exactly as before, and partner integrations (Phase 8) will not use this flow.

### Flow

```text
POST /auth/login (admin email + password)
  → 200 { "mfa_required": true, "mfa_enrollment_required": <bool>,
          "mfa_token": "<5-minute token>", "expires_in": 300 }
     (no access token, no cookie)

First time (mfa_enrollment_required = true):
  POST /auth/mfa/enroll/start    { mfa_token }        → { secret, otpauth_uri, issuer, account }
       app shows otpauth_uri as a QR code (Google Authenticator, 1Password, Authy…)
  POST /auth/mfa/enroll/confirm  { mfa_token, code }  → tokens (aal=2) + cookie + 10 recovery_codes (shown once)

Every later login:
  POST /auth/mfa/verify  { mfa_token, code }            → tokens (aal=2) + cookie
                     or  { mfa_token, recovery_code }

Signed-in admin (aal=2):
  POST /auth/mfa/recovery-codes  → 10 new codes; all old codes stop working
```

All `/auth/mfa/*` routes except `recovery-codes` are cookie routes: they need `X-Rejecta-Client: web`.

### How it is protected

- **The MFA token** is a separate JWT type, with its own `typ` header and its own audience.
  - It can never be used as an access token, and an access token can never be used as an MFA token.
  - It lasts 5 minutes and is bound to the admin's `token_version`, so a password or role change voids it.
  - It is single-use, and limited to 5 code attempts. On top of that, an admin gets at most 10 failed codes per 15 minutes across all tokens.
- **TOTP:**
  - Standard RFC 6238: 6 digits, 30-second steps, ±1 step allowed for clock drift.
  - Secrets are 160-bit and encrypted at rest with Fernet (`MFA_ENCRYPTION_KEY`).
  - Replay protection: a time step can be used only once, enforced atomically in the database (`mfa_last_step`).
- **Enrollment:** the secret stays in a separate *pending* column until it is confirmed with a valid code. A half-finished setup never enables MFA and never signs anyone out.
- **Recovery codes:**
  - 10 single-use codes of 50 random bits each, in the form `ABCDE-FGHJK`. Input ignores case, spaces and dashes.
  - Stored as HMAC-SHA256 with a key derived from `MFA_ENCRYPTION_KEY`, so a database leak alone does not reveal them.
- **Admin routes** use the `require_admin` dependency. It needs `role = admin` read from the database, plus `aal = 2`, plus MFA enabled. Refreshed admin sessions keep aal=2.
- **Changes sign the admin out:** enabling or resetting MFA bumps `token_version`, which ends every earlier session.
- **Audit log:** `auth.mfa_challenge_issued`, `auth.mfa_enrolled`, `auth.mfa_failed`, `auth.login` (with the method and how many recovery codes are left), and `auth.mfa_recovery_codes_regenerated`.

### Break-glass

If an admin loses both the phone and the recovery codes, someone with server access runs:

```bash
python -m scripts.create_admin --email admin@example.com --reset-mfa
```

This removes MFA, signs the admin out everywhere, and makes the next login start enrollment again. In Phase 5, another admin will be able to do the same from the admin panel.

### Tests added

| Suite | Count | What it covers |
|---|---|---|
| Unit: MFA | 8 | Secret encryption and wrong key, drift window, code input, otpauth URI, recovery codes (format, normalisation, keyed hash), MFA token and access token isolation. |
| Integration: admin MFA | 13 | No session from password alone, the full enrollment flow, TOTP replay rejection, single-use tokens and recovery codes, recovery-code regeneration, the attempt limit (6th try returns 429), token-type swapping, `token_version` binding, `require_admin` (401/403/aal1), MFA reset, request validation, users unaffected. |

The full suite now has **120 passed** and 3 skipped. The 3 skipped are older async tests for the calibrator, forge and rebuttal tools; they need an async pytest plugin and are not related to auth.

---

## 7. Phase 4: feature gating and quotas ✅

**Which endpoints need what:**

| Endpoint | Access | Feature key |
|---|---|---|
| `POST /analyse-paper`, `POST /analyse` | user or partner token, plus quota | `analyse_paper` |
| `POST /voice-calibrate` | user or partner token, plus quota | `voice_calibrate` |
| `POST /abstract-forge/generate` | user or partner token, plus quota | `abstract_forge` |
| `POST /rebuttal-writer/analyze` | user or partner token, plus quota | `rebuttal_writer` |
| `POST /ai-signal-heatmap/analyze` | user or partner token, plus quota | `ai_heatmap` |
| `POST /parse`, `/citations`, `/journal-match` | public, 30 per minute per IP | — |
| `GET /health` | public | — |

**How a metered call works** ([auth/usage.py](../rejecta-api/auth/usage.py)):

1. **Resolve the feature.** Check the global switch, then the plan row, then any non-expired user override. If the feature is off, return `403 feature_disabled` or `403 feature_not_in_plan`.
2. **Reserve one unit atomically.** `consume_quota` checks the daily and the monthly limit together. If either is exhausted, return `429 quota_exceeded` with `feature`, `period`, `limit`, `used`, `resets_at` and a `Retry-After` header.
3. **Run the tool.**
   - If it succeeds, the unit is committed.
   - If it fails (bad PDF, LLM error, client disconnect), the unit is refunded.
   - Cheap validation runs before step 2, so bad input never uses quota.
4. **Startup cleanup.** Reservations left open by a crash or redeploy are refunded when the API starts.

**`GET /me`** now includes, for every feature: enabled or not, the limits, the usage today and this month, what remains, and when it resets.

**CORS:** only the app origins are allowed (`rejecta.scivalon.com`, localhost, `CORS_ORIGINS`), plus Netlify deploy previews of this site. Before this change, the API allowed any origin.

## 8. Phase 5: admin API ✅

All `/admin/*` routes need `require_admin`: role admin plus a two-factor session (aal=2). Every write is recorded in the audit log with the before and after values.

| Area | Routes |
|---|---|
| Users | `GET /admin/users` (search, role, status, plan, paging) · `POST /admin/users` (returns a one-time password-setup link) · `GET/PATCH /admin/users/{id}` · `POST …/password-reset-link` · `POST …/revoke-sessions` · `POST …/mfa/reset` · `PUT/DELETE …/overrides/{feature}` · `POST …/usage/reset` |
| Plans | `GET/POST /admin/plans` · `GET/PATCH /admin/plans/{id}` (rename, default, archive/restore) · `PUT …/features` (whole matrix at once) · `POST …/duplicate` · `POST …/migrate-users` · `GET …/usage?period=day\|month` |
| Features | `GET /admin/features` · `PATCH /admin/features/{key}` (rename, global kill switch) |
| Partners | `GET /admin/partners?search=&status=&page=&page_size=` (paginated) · `POST /admin/partners` · `GET/PATCH /admin/partners/{id}` · `POST …/secrets` · `DELETE …/secrets/{id}` |
| Insights | `GET /admin/stats` · `GET /admin/usage?days=` · `GET /admin/audit-log?action=&target_id=&before_id=` |
| Users (public) | `POST /auth/reset-password` (token from an admin link, one use, 24 hours) |

**Rules:**
- **Optimistic locking on plans.** Every plan change sends the `version` it was based on. If another admin saved in between, the request gets `409 version_conflict` instead of overwriting their change.
- **Enforced in the database:** exactly one default plan; a plan that users or partners are on cannot be archived; daily ≤ monthly; the last active admin cannot be demoted or suspended.
- **Your own account:** an admin cannot change their own role or status.
- **Password links:** admins never see or set passwords. New accounts and resets get a link instead.
- **Signing out:** changing a role or status, revoking sessions, or resetting MFA signs the user out everywhere.

## 9. Phases 6 & 7: frontend ✅

**Auth core:**
- [`src/auth/client.ts`](../rejecta-frontend/src/auth/client.ts):
  - The access token lives in memory only, never in localStorage.
  - Refresh is single-flight across tabs (Web Locks).
  - A 401 triggers one silent refresh and retry.
  - Calls whose response sets the cookie (change-password, logout-all) go through the auth proxy, so the cookie stays first-party.
- [`AuthProvider.tsx`](../rejecta-frontend/src/auth/AuthProvider.tsx): restores the session on load, handles the MFA steps, and re-reads `/me` after each tool call.
- **Guards:** `RequireAuth` and `RequireAdmin` only shape the UI. The API checks everything on its own.

**Pages:**
- `/login` handles admin two-factor: QR setup, codes, and recovery codes with copy and download.
- `/signup`, `/reset-password`.
- `/account`: usage bars, change password, sign out everywhere, and new recovery codes for admins.

**Tool pages:** all five tool pages need sign-in. A thin strip above each shows "*N of M left today*", amber when low and red when used up.

**Admin console** (`/admin`, loaded only when an admin opens it): Overview, Users (search and filters, create with a setup link), User detail (plan, role, status, reset link, sign-out, MFA reset, per-feature usage, override editor), **Plans** (cards), **Plan editor**, Features (kill switch), Partners (create with the secret shown once, secrets rotation, scopes), Usage (chart and tables), Audit log.

**The plan editor** has:
- A feature matrix with an on/off toggle and daily and monthly limits, each with an "Unlimited" option.
- Validation that daily ≤ monthly.
- Unsaved-changes tracking, a Discard button, and a warning before leaving the page.
- A reload prompt when another admin saved first (version conflict).
- Actions: make default, archive/restore, duplicate, move all users to another plan.
- Live usage for the current day or month.

**Proxies:**
- Vite (dev) and Netlify (prod) send `/api/auth/*` to the API's `/auth/*`.
- The Netlify proxy is signed (`X-Nf-Sign`), so the API can trust the client IP Netlify forwards.
- Netlify also sets the security headers and a strict Content-Security-Policy (scripts only from the site itself).

**A fix to existing behaviour:** the Heatmap and Abstract Forge pages used to run the tool automatically on page load (twice in development). With quotas, every visit cost the user credits. They now run only when the user clicks.

**Browser test:**
- Done end to end on a local stack (Vite, the API, a Postgres container). Covered: sign-up, session restore after reload, a tool run with the counter updating live, sign-out, admin first login with QR enrollment and recovery codes, the admin dashboard, editing Free-plan limits in the plan editor, and the change reaching both the database and the audit log.
- Every admin page loaded with no browser console errors.

## 10. Phase 8: partner integrations (ChemLabFlow) ✅

This uses OAuth2 client credentials. The partner guide, with curl, Python and Node examples, is in [PARTNER_INTEGRATION.md](PARTNER_INTEGRATION.md).

- **Credentials:** an admin creates a partner with a plan (its limits, one pool for the whole partner) and scopes (which tools it may call). The client secret `rjs_…` is shown once; only its SHA-256 is stored.
- **Tokens:** `POST /oauth/token` (form or HTTP Basic) returns a 15-minute `partner+jwt` token.
  - It works **only** on the tool endpoints. `/me`, `/auth/*` and `/admin/*` reject it.
  - Scopes are checked on every call (`403 insufficient_scope`).
- **Rotation:** at most 2 active secrets, so a partner can rotate without downtime.
- **Ending tokens:** revoking a secret, disabling the partner, or changing its plan or scopes ends every token it holds.
- **Rate limits:** 30 token requests per minute per IP, and 10 failed authentications per client per 15 minutes.
- **Not built yet:** per-end-user limits inside a partner. This was left for later on purpose; the usage tables already support it.

## 11. Phase 9: hardening ✅

- **Strict production startup:** with `ENVIRONMENT=production`, the API refuses to start if any of these is wrong: missing DB, JWT or MFA keys; `AUTH_COOKIE_SECURE=false`; `DOCS_ENABLED=true`; database TLS off; `APP_URL` not https.
- **API docs off in production:** `/docs` and `/openapi.json` are hidden when `DOCS_ENABLED=false`.
- **Response headers:** `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, HSTS in production, and `Cache-Control: no-store` on `/auth`, `/admin`, `/me` and `/oauth`.
- **Request ids:** every response carries an `X-Request-Id`, which also appears in the logs. Tokens are never logged.
- **Abandoned reservations:** quota reservations left open by a crash are refunded at startup.
- **Final tests:** **173 passed** (3 old async tests skipped, unrelated), on a Postgres 17 container. The suite wipes and rebuilds that database, and it refuses Supabase hosts. `scripts.verify_db` passes 51/51 on Supabase. The frontend type-checks and builds.

## 12. Production deploy checklist

> ⚠️ **Deploy the backend and the frontend together.** Once the new API is live, the tools require sign-in, and the old frontend has no login. Do all the steps below in one window.

**A. Before deploying**
1. **Reset the Supabase `postgres` password.** It was shared in a chat.
2. **Get the Session pooler URL.** In Supabase, go to Connect → **Session pooler** and copy the string. Railway has no IPv6, so the direct `db.<ref>.supabase.co` host will not work there. The user is `postgres.<project-ref>`.
3. **Create the API role password.** Run this locally:
   ```
   MIGRATION_DATABASE_URL=<pooler URL> python -m scripts.db_role
   ```
   It prints the runtime `DATABASE_URL`, with user `rejecta_api.<project-ref>`.
4. **Generate shared secrets.** Create one random secret for the Netlify proxy signature, for example with `python -c "import secrets;print(secrets.token_urlsafe(32))"`.

**B. Railway (API) variables**

| Variable | Value |
|---|---|
| `ENVIRONMENT` | `production` |
| `DATABASE_URL` | from step A3 |
| `MIGRATION_DATABASE_URL` | only if you run migrations from Railway; otherwise leave it out |
| `DB_SSL_MODE` | `require` |
| `JWT_PRIVATE_KEY`, `JWT_KEY_ID` | the same values as in your local `.env`, or new ones from `scripts.generate_keys` (existing sessions are then signed out) |
| `MFA_ENCRYPTION_KEY` | **the same value as local `.env`** (it decrypts the admins' 2FA secrets) |
| `JWT_ISSUER` | `https://rejecta-production.up.railway.app` |
| `APP_URL`, `FRONTEND_URL` | `https://rejecta.scivalon.com` |
| `AUTH_COOKIE_SECURE` | `true` |
| `AUTH_COOKIE_PATH` | `/api/auth` |
| `DOCS_ENABLED` | `false` |
| `NETLIFY_PROXY_SECRET` | from step A4 |
| plus the existing LLM variables | unchanged |

**C. Netlify (frontend) variables**

| Variable | Value |
|---|---|
| `VITE_API_URL` | `https://rejecta-production.up.railway.app` |
| `VITE_AUTH_BASE` | `/api/auth` |
| `NETLIFY_PROXY_SECRET` | the same value as on Railway |

**D. Deploy and verify**
1. Deploy the API, then the frontend.
2. `GET /health` should return `"database": "ok"`, and `/docs` should return 404.
3. Create the first admin: `python -m scripts.create_admin --email … --name …`. This runs against the Supabase DB from a machine with `DATABASE_URL` set.
4. Sign in at `https://rejecta.scivalon.com/login` and set up 2FA. Save the recovery codes somewhere safe.
5. Check sign-up in a private window, one tool run, and that the usage strip updates.
6. Run `python -m scripts.verify_db` once more against Supabase.

## 13. Running it locally

```bash
cd rejecta-api
pip install -r requirements-dev.txt

# one-time setup (already done for the current Supabase project)
alembic upgrade head
python -m scripts.db_role --write-env
python -m scripts.generate_keys --write-env
python -m scripts.create_admin --email you@example.com --name "Your Name"

uvicorn main:app --reload
```

Frontend, in a second terminal (Vite proxies `/api/auth` to `localhost:8000`):

```bash
cd rejecta-frontend
npm install
cp .env.example .env
npm run dev          # http://localhost:5173 → /signup, /login, /admin
```

Try it with curl:

```bash
curl -X POST http://localhost:8000/auth/signup \
  -H "Content-Type: application/json" -H "X-Rejecta-Client: web" \
  -d '{"email":"me@example.com","password":"my lab notebook","full_name":"Me"}'

curl http://localhost:8000/me -H "Authorization: Bearer <access_token>"
```

The refresh cookie path is `/api/auth`, which matches the browser path through the Netlify and Vite proxies. To test `/auth/refresh` directly against `localhost:8000`, set `AUTH_COOKIE_PATH=/auth` in your local `.env`.

**Integration tests** need a disposable Postgres. The test fixture wipes it, and refuses to run against Supabase hosts:

```powershell
docker run -d --name rejecta-test-db -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:55432:5432 postgres:17
$env:TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:55432/postgres"
python -m pytest
```

## 14. Configuration reference (backend)

| Variable | Default | Notes |
|---|---|---|
| `MIGRATION_DATABASE_URL` | — | `postgres` owner URL. Used for migrations and scripts only. |
| `DATABASE_URL` | — | `rejecta_api` URL. Generated by `scripts.db_role`. |
| `DB_SSL_MODE` | `require` | Use `disable` only for local test Postgres. |
| `JWT_PRIVATE_KEY`, `JWT_KEY_ID` | — | Generated by `scripts.generate_keys`. Secret. |
| `JWT_PREVIOUS_PUBLIC_KEYS` | empty | Old keys, used during rotation. |
| `JWT_ISSUER` | Railway URL | |
| `JWT_AUDIENCE` | `rejecta-api` | |
| `ACCESS_TOKEN_TTL_MINUTES` | 15 | Allowed range is 5 to 60. |
| `REFRESH_TOKEN_TTL_DAYS` | 30 | Sliding. |
| `SESSION_MAX_LIFETIME_DAYS` | 90 | Absolute. |
| `AUTH_COOKIE_PATH` | `/api/auth` | |
| `AUTH_COOKIE_SECURE` | `true` | `false` only for local http. |
| `NETLIFY_PROXY_SECRET` | empty | Lets the API trust Netlify's client IP header. |
| `LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_MINUTES` | 5, 15 | |
| `MFA_ENCRYPTION_KEY` | — | Fernet key, generated by `scripts.generate_keys`. Secret. Rotating it forces every admin to re-enroll. |
| `MFA_ISSUER_NAME` | `Rejecta` | The name shown in authenticator apps. |
| `MFA_TOKEN_TTL_MINUTES` | 5 | How long the step between password and code may take. |
| `ENVIRONMENT` | `development` | Set `production` on Railway to turn on the startup safety checks. |
| `DOCS_ENABLED` | `true` | Must be `false` in production. |
| `CORS_ORIGINS` | empty | Extra browser origins, comma-separated. |
| `CORS_ORIGIN_REGEX` | Netlify previews of `rejectaa` | Pattern for extra origins. |
| `APP_URL` | `http://localhost:5173` | The base of password-setup and reset links; https in production. |

## 15. Known limits and follow-ups

- **Single instance:** rate limits and the short caches (user, plan entitlements, partner) are per process, which suits one Railway instance. With several instances, move them to Redis; the interfaces are already isolated.
- **No email yet:** there is no email verification and no self-service "forgot password"; admins send reset links instead. Adding an email provider later only needs a new endpoint that sends the same kind of link.
- **Partner end users:** per-end-user limits inside a partner are not built (by decision). Today each partner is one pool.
- **Sign-up enumeration:** sign-up reveals that an email is already registered. This cannot be avoided without email verification, and sign-up is rate-limited.
- **Google login:** deferred, as decided.
- **Old async tests:** the 3 skipped tests (calibrator, forge, rebuttal) need an async pytest plugin. They predate this work.
