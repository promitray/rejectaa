# Rejecta: access control, plans and admin console. Implementation report

**Date:** 3 Nov 2026  **Branch:** `feat/auth-system`  **Status:** built and tested locally, database migrated on Supabase, **not committed and not deployed**.

> **Viewing the diagrams:** the diagrams are Mermaid. They render on GitHub and GitLab. In VS Code, install the extension *Markdown Preview Mermaid Support*, then open the preview (Ctrl+Shift+V). Every diagram is followed by a plain-text explanation, so the report also reads fine without them.

---

## 1. Summary

Rejecta started as an open API: anybody could call any tool, there were no accounts, and the database did not exist. Today it got a complete access-control system.

| Area | What now exists |
|---|---|
| Accounts | Sign-up, sign-in, sign-out, sign out of all devices, change password, admin-issued password reset links |
| Auth engine | Our own JWT (Ed25519), rotating refresh tokens with theft detection, Argon2id passwords. **Supabase is used only as the Postgres database.** |
| Roles | `admin` and `user`. Admins must use two-factor authentication (TOTP) and get the unlimited **Lab** plan by default |
| Plans and limits | Plans, features, daily and monthly limits, per-user overrides, a global kill switch per feature, all editable by an admin |
| Quotas | Atomic, race-free counters. If a tool call fails, the user's credit is refunded |
| Admin console | 9 pages: overview, users, user detail, plans, plan editor, features, partners, usage, audit log |
| Partner API | OAuth2 client credentials for platforms such as ChemLabFlow: scoped, rotatable secrets, own plan and limits |
| Frontend | Login, sign-up, reset and account pages, a usage strip on every tool, show/hide password, frosted-glass loaders, server-side pagination |
| Hardening | Strict CORS, security headers, startup safety checks, request ids, audit log of every sensitive action |

**By the numbers**

| | |
|---|---|
| API routes | 53 (29 admin, 11 auth, 10 tools and health, 3 other) |
| Database | 13 tables in schema `app`, 7 Alembic migrations, 2 SQL functions for quotas |
| Backend tests | **173 passed**, 3 skipped (old async tests unrelated to this work) |
| Database checks | **51 of 51** security and integrity checks pass on Supabase |
| Code written | about 5,500 lines of Python (auth, routers, db, scripts) and about 3,600 lines of TypeScript (new frontend files) |
| Frontend | type-check and production build pass |

---

## 2. How we got here: decisions

```mermaid
flowchart TD
    A["Goal: roles admin and user,<br/>JWT auth, usable by ChemLabFlow,<br/>Supabase as database"] --> B{"First plan:<br/>Supabase Auth"}
    B -->|"Decision: build our own JWT auth,<br/>Supabase = database only"| C["Own auth in FastAPI"]
    C --> D{"Migrations tool?"}
    D -->|"Decision: Alembic<br/>(raw SQL mode)"| E["7 migrations"]
    C --> F{"Email features?"}
    F -->|"Decision: no email verification,<br/>no email reset for now"| G["Admin-issued reset links"]
    C --> H{"2FA?"}
    H -->|"Decision: admins only,<br/>users and partners unaffected"| I["TOTP for admins"]
    C --> J{"Partner limits?"}
    J -->|"Decision: one pool per partner,<br/>per-end-user limits later"| K["Client credentials + plan"]
    C --> L{"Admin limits?"}
    L -->|"Decision: admins start on Lab,<br/>unlimited"| M["Role stays separate from plan"]
```

Decisions that were not made, on purpose: Google login, an email provider, and per-end-user limits inside a partner. All three can be added later without redesign.

---

## 3. System architecture

```mermaid
flowchart LR
    subgraph Browser["Browser: rejecta.scivalon.com"]
        UI["React app<br/>access token kept in memory only"]
    end
    subgraph Netlify["Netlify"]
        STATIC["Static site<br/>security headers + CSP"]
        PX["/api/auth/* signed proxy<br/>refresh cookie stays first-party"]
    end
    subgraph Railway["Railway: FastAPI"]
        AUTHR["/auth/*  /me"]
        MFAR["/auth/mfa/*"]
        ADMR["/admin/*<br/>admin + 2FA only"]
        TOOLS["5 metered tools<br/>login or partner token + quota"]
        PUBLIC["3 free tools<br/>IP rate limit"]
        OAUTH["/oauth/token"]
    end
    DB[("Supabase Postgres<br/>schema app, RLS on")]
    PART["Partner server<br/>e.g. ChemLabFlow"]

    UI --> STATIC
    UI -->|"login, refresh, logout"| PX --> AUTHR
    PX --> MFAR
    UI -->|"Bearer access token"| ADMR
    UI -->|"Bearer access token"| TOOLS
    UI --> PUBLIC
    PART -->|"client id + secret"| OAUTH
    PART -->|"Bearer partner token"| TOOLS
    AUTHR --> DB
    MFAR --> DB
    ADMR --> DB
    TOOLS --> DB
    OAUTH --> DB
```

**Why auth goes through Netlify.** The API lives on `railway.app` and the site on `scivalon.com`. A cookie set by Railway would be a third-party cookie, which Safari and others block. The Netlify proxy makes the refresh cookie belong to the site itself.

**Why tools go direct.** Analysis calls take 15 to 45 seconds. Sending them straight to Railway avoids proxy timeouts.

---

## 4. Database

Supabase is used only as Postgres. Everything lives in schema `app`, which Supabase's public REST API does not expose. Row Level Security is on for every table and Supabase's own roles (`anon`, `authenticated`, `service_role`) have no access at all. The API connects as a separate low-privilege role, `rejecta_api`.

```mermaid
erDiagram
    PLANS ||--o{ USERS : "is on"
    PLANS ||--o{ PLAN_FEATURES : "defines"
    FEATURES ||--o{ PLAN_FEATURES : "limited in"
    USERS ||--o{ REFRESH_TOKENS : "sessions"
    USERS ||--o{ USER_OVERRIDES : "custom limits"
    FEATURES ||--o{ USER_OVERRIDES : "for"
    USERS ||--o{ MFA_RECOVERY_CODES : "admin only"
    USERS ||--o{ PASSWORD_RESET_TOKENS : "admin issued"
    USERS ||--o{ AUDIT_LOG : "actor"
    PLANS ||--o{ API_CLIENTS : "partner plan"
    API_CLIENTS ||--o{ API_CLIENT_SECRETS : "rotatable"

    PLANS {
        uuid id
        text key
        bool is_default
        text status
        int version
    }
    USERS {
        uuid id
        text email
        text password_hash
        text role
        text status
        int token_version
        bool mfa_enabled
    }
    PLAN_FEATURES {
        uuid plan_id
        text feature_key
        bool enabled
        int daily_limit
        int monthly_limit
    }
    REFRESH_TOKENS {
        uuid family_id
        bytes token_hash
        timestamp expires_at
        timestamp revoked_at
    }
    USAGE_COUNTERS {
        text subject_type
        uuid subject_id
        text feature_key
        text period
        int count
    }
    USAGE_EVENTS {
        bigint id
        text status
        text feature_key
    }
    AUDIT_LOG {
        bigint id
        text action
        jsonb before
        jsonb after
    }
```

`USAGE_COUNTERS` and `USAGE_EVENTS` point at a user or a partner by id without a foreign key, because the subject can be either.

**Migrations (Alembic, raw SQL)**

| # | Content |
|---|---|
| 0001 | Schema `app`, the `rejecta_api` role, lockdown of Supabase roles |
| 0002 | 11 tables with constraints and triggers |
| 0003 | `consume_quota` and `finalize_usage` (atomic quotas with refunds) |
| 0004 | Seed: 5 features, 4 plans |
| 0005 | Absolute session lifetime |
| 0006 | Pending TOTP secret during enrollment |
| 0007 | Partner tables `api_clients` and `api_client_secrets` |

**Rules the database enforces by itself, whatever the API does**

- Exactly one active default plan exists at any time.
- A plan that still has users or partners cannot be archived or deleted.
- New users without a plan get the default plan.
- Changing a role, status, password or 2FA setting raises `token_version`, which invalidates every earlier token.
- The last active admin can never be demoted, suspended or deleted (concurrency-safe).
- Emails are stored trimmed and lower-case, and are unique.
- `audit_log` is append-only for the API role.
- Daily limit cannot exceed monthly limit.

**Starter plans (admins edit these in the console)**

| Feature | Free (default) | Researcher | Lab | Partner |
|---|---|---|---|---|
| Paper analysis | 3 / day | 20 / month | unlimited | unlimited |
| Voice Calibrator | 5 / day | 100 / month | unlimited | unlimited |
| Abstract Forge | 5 / day | 100 / month | unlimited | unlimited |
| Rebuttal Writer | 3 / day | 50 / month | unlimited | unlimited |
| AI Signal Heatmap | 20 / day | unlimited | unlimited | unlimited |

---

## 5. Authentication flows

### 5.1 Tokens at a glance

| Token | Form | Lifetime | Stored where |
|---|---|---|---|
| Access token | JWT, Ed25519 | 15 minutes | browser memory only, never localStorage |
| Refresh token | random 256-bit string `rt_…` | 30 days sliding, 90 days absolute | HttpOnly, Secure, SameSite=Strict cookie. The database keeps only its SHA-256 |
| MFA token | JWT, separate type and audience | 5 minutes, single use | memory, during admin sign-in |
| Partner token | JWT, separate type and audience | 15 minutes | partner's server |

Verification rejects `alg=none`, key-confusion (HS256), unknown key ids, wrong issuer or audience, expired or not-yet-valid tokens, and malformed claims. On every request the API also re-reads the user from the database, so a suspended user or a changed role takes effect immediately, not when the token expires.

### 5.2 Sign-up and sign-in (regular user)

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant N as Netlify proxy
    participant A as API
    participant D as Database

    B->>N: POST /api/auth/login (email, password, X-Rejecta-Client: web)
    N->>A: POST /auth/login
    A->>A: CSRF guard, IP rate limit, per-email limit
    A->>D: find user by email
    alt unknown email
        A->>A: verify a dummy hash (same time cost)
        A-->>B: 401 "Invalid email or password."
    else known user
        A->>A: Argon2id verify
        alt wrong password
            A->>D: failed count +1 (lock after 5)
            A-->>B: 401 "Invalid email or password."
        else correct and active
            A->>D: create session, audit "auth.login"
            A-->>N: 200 access token + Set-Cookie refresh (HttpOnly)
            N-->>B: same, cookie belongs to the site
        end
    end
```

Sign-up works the same way but creates the account first (role `user`, default plan). A request body that contains `role` or `plan_id` is rejected, so nobody can sign themselves up as admin. The first admin is created only by a script run on the server; after that, only an existing admin (who has passed two-factor sign-in) can create or promote another one.

### 5.3 Admin sign-in with two-factor authentication

```mermaid
flowchart TD
    S["Admin enters email + password"] --> P{"Password correct?"}
    P -->|no| F["401 generic error<br/>failure counted, lock after 5"]
    P -->|yes| M["Issue 5-minute MFA token<br/>(no session, no cookie yet)"]
    M --> E{"2FA already set up?"}
    E -->|"no, first login"| ES["enroll/start:<br/>QR code + setup key<br/>(secret kept encrypted, pending)"]
    ES --> EC["enroll/confirm with a 6-digit code"]
    EC -->|"code valid"| R["2FA enabled<br/>10 recovery codes shown once<br/>session with aal=2"]
    EC -->|"code wrong"| X["401, attempt counted"]
    E -->|yes| V["verify: 6-digit code<br/>or one recovery code"]
    V -->|"valid and not replayed"| OK["session with aal=2"]
    V -->|"wrong, reused, or token used up"| X
    R --> ADM["/admin/* allowed:<br/>role=admin AND aal=2"]
    OK --> ADM
```

- A password alone never gives an admin a session.
- An MFA token cannot be used as an access token, and the other way round.
- A time step of the authenticator can be used only once (replay protection). Recovery codes work once each.
- 5 attempts per MFA token, and 10 failed codes per admin in 15 minutes.
- If an admin loses the phone and the recovery codes, someone with server access runs `create_admin --reset-mfa`.

### 5.4 Refresh tokens: rotation and theft detection

```mermaid
flowchart TD
    A["POST /auth/refresh with cookie"] --> B{"Token found?"}
    B -->|no| X["401 invalid_refresh_token<br/>cookie cleared"]
    B -->|yes| C{"Already rotated?"}
    C -->|"yes, less than 10 s ago"| G["401 refresh_superseded<br/>(two tabs refreshed together)<br/>nothing revoked, client retries once"]
    C -->|"yes, older"| T["THEFT SUSPECTED<br/>revoke the whole session<br/>write audit event"]
    C -->|no| D{"Expired, or 90-day limit,<br/>or user suspended,<br/>or token_version changed?"}
    D -->|yes| R["revoke the session, 401"]
    D -->|no| N["Create new refresh token,<br/>mark the old one rotated,<br/>return new access token"]
```

### 5.5 Passwords and brute force

- **Hashing:** Argon2id, run in worker threads (at most 4 at once, to bound memory).
- **Policy:** 10 to 128 characters; common passwords, repetitive passwords and the user's own email are refused. No forced symbols, following NIST guidance.
- **Limits:** sign-up 5 per hour per IP, sign-in 20 per 5 minutes per IP, 5 failures per email in 15 minutes (account lock stored in the database, so a restart does not clear it).
- **Same message for unknown and known emails**, with the same response time.

### 5.6 Password reset without email

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant AD as Admin
    participant API as API
    participant DB as Database

    U->>AD: "I forgot my password"
    AD->>API: POST /admin/users/{id}/password-reset-link
    API->>DB: store hash of one-time token (24 h), cancel older links
    API-->>AD: link (shown once)
    AD->>U: sends the link
    U->>API: POST /auth/reset-password (token, new password)
    API->>DB: set new hash, token_version +1, revoke all sessions, mark link used
    API-->>U: 204
    U->>API: signs in normally
```

The same mechanism is used when an admin creates a new account. The admin never sees or chooses the password.

---

## 6. Authorization and quotas

### 6.1 What happens on every tool call

```mermaid
flowchart TD
    R["Tool request<br/>Authorization: Bearer ..."] --> T{"Token type"}
    T -->|"user access token"| U["Verify signature and claims<br/>load user: active? same token_version?"]
    T -->|"partner token"| PT["Verify signature and claims<br/>load client: active? same token_version?<br/>scope includes this tool?"]
    T -->|"missing or invalid"| E1["401"]
    U --> V["Cheap input validation<br/>(bad input never uses quota)"]
    PT --> V
    V --> EN{"Feature enabled for this subject?"}
    EN -->|"switched off for everyone"| E2["403 feature_disabled"]
    EN -->|"not in plan or override off"| E3["403 feature_not_in_plan"]
    EN -->|yes| Q{"Reserve one unit atomically<br/>daily AND monthly"}
    Q -->|"limit reached"| E4["429 quota_exceeded<br/>period, limit, used, resets_at, Retry-After"]
    Q -->|"reserved"| W["Run the tool"]
    W -->|"success"| C["Commit the unit"]
    W -->|"error, bad PDF, model failure,<br/>client disconnect"| RF["Refund the unit"]
    C --> OUT["Response"]
    RF --> OUT2["Error response, user not charged"]
```

20 parallel requests against a limit of 3 let exactly 3 through; this is tested. Reservations left open by a crash are refunded when the API starts. Daily limits reset at 00:00 UTC and monthly limits on the 1st (UTC).

### 6.2 How a feature's access is decided

```mermaid
flowchart LR
    G{"Global switch<br/>feature on?"} -->|off| D1["Disabled for everyone"]
    G -->|on| P{"Plan row:<br/>included?"}
    P -->|no| O{"User override<br/>forces it on?"}
    P -->|yes| O2["Plan limits"]
    O -->|no| D2["Not in plan"]
    O -->|yes| L["Access, limits from override"]
    O2 --> OV{"Override sets<br/>different limits?"}
    OV -->|yes| L
    OV -->|no| L2["Access, plan limits"]
```

An override can change on/off, the daily limit, the monthly limit, or any mix of them, optionally with an expiry date and a reason. A `NULL` limit means unlimited. The global switch always wins.

### 6.3 Roles and plans are separate

| | Role | Plan |
|---|---|---|
| Decides | what you may manage (admin console) | how much of each tool you may use |
| Values | `admin`, `user` | Free, Researcher, Lab, Partner, and any plan admins create |
| Admins | need 2FA | start on **Lab** (unlimited); their usage is still counted |

Where the admin default applies: a new admin created by the script, a user promoted by the script, a new admin created in the console, and a user promoted in the console (unless the admin picks another plan). Demoting an admin keeps their plan.

---

## 7. Admin console

### 7.1 Page map

```mermaid
flowchart LR
    ADM["/admin"] --> OV["Overview<br/>stats, users by plan"]
    ADM --> US["Users<br/>search, filters, paging, create"]
    US --> UD["User detail<br/>profile, plan, role, status,<br/>reset link, sign out, reset 2FA,<br/>usage per feature, overrides"]
    ADM --> PL["Plans<br/>cards"]
    PL --> PE["Plan editor<br/>feature matrix, limits, default,<br/>archive, duplicate, move users"]
    ADM --> FE["Features<br/>kill switch, rename"]
    ADM --> PA["Partners<br/>search, filter, paging, create"]
    PA --> PD["Partner detail<br/>settings, scopes, secrets, usage"]
    ADM --> UG["Usage<br/>chart, by feature, by plan"]
    ADM --> AU["Audit log<br/>filters, load older"]
```

The console is loaded only when an admin opens it, so regular users never download that code.

### 7.2 Plan management in detail

The plan editor is the page for "manage all plans, their usage limits and which features they give".

- A matrix: one row per feature, with an on/off switch, a daily limit and a monthly limit. Each limit has an *Unlimited* checkbox.
- Validation before saving: daily limit cannot be higher than monthly.
- Unsaved changes are tracked, with a Discard button and a warning before leaving the page.
- Actions: make default for new sign-ups, archive and restore, duplicate, move all users and partners to another plan.
- Live usage of the plan for today or this month.
- A saved change applies from the next request. The server clears its caches.

```mermaid
sequenceDiagram
    autonumber
    participant A1 as Admin 1 (page)
    participant A2 as Admin 2 (page)
    participant API as API
    participant DB as Database

    A1->>API: GET plan (version 4)
    A2->>API: GET plan (version 4)
    A1->>API: PUT features (version 4)
    API->>DB: bump version to 5 only if still 4, save matrix, audit
    API-->>A1: saved (version 5)
    A2->>API: PUT features (version 4)
    API->>DB: version is not 4 any more
    API-->>A2: 409 version_conflict
    A2->>A2: "Another admin changed this plan. Reload the latest version."
```

Safety rules enforced in the database: exactly one default plan; a plan with users cannot be archived (move them first); the last admin cannot be removed. In the API: an admin cannot change their own role or status.

### 7.3 Audit log

Every sign-in, failed sign-in, 2FA event, token-theft alert, partner token and admin change is recorded with the actor, the target, the before and after values, the IP address and the browser. Entries cannot be edited or deleted by the API.

---

## 8. Partner integration (ChemLabFlow)

```mermaid
sequenceDiagram
    autonumber
    participant AD as Rejecta admin
    participant API as Rejecta API
    participant P as Partner server

    AD->>API: create partner (plan, scopes)
    API-->>AD: client id + client secret (shown ONCE)
    AD->>P: hands over credentials securely
    P->>API: POST /oauth/token (client credentials)
    API-->>P: 15-minute partner token
    loop every tool call
        P->>API: POST /analyse-paper  Bearer token
        API->>API: scope check, plan limits, partner pool quota
        API-->>P: result, or 403 / 429 with details
    end
    Note over P,API: Rotation: admin adds a second secret, partner switches, admin revokes the old one. Revoking ends all its tokens.
```

- Partner tokens work only on the tool endpoints, never on `/me`, `/auth/*` or `/admin/*`.
- One shared usage pool per partner, limited by the plan assigned to it.
- At most two active secrets; only a SHA-256 of each is stored.
- Disabling a partner, or changing its plan or scopes, ends its tokens.
- Guide for the partner's developers, with curl, Python and Node examples: [PARTNER_INTEGRATION.md](PARTNER_INTEGRATION.md).

---

## 9. Frontend

### 9.1 Structure

```mermaid
flowchart TD
    APP["App.tsx<br/>routes"] --> AP["AuthProvider<br/>session, MFA steps, usage refresh"]
    AP --> CL["auth/client.ts<br/>token in memory, one refresh at a time,<br/>one retry on 401, errors with codes"]
    AP --> GD["Guards<br/>RequireAuth, RequireAdmin"]
    GD --> TP["Tool pages (5)<br/>usage strip: N of M left"]
    GD --> AC["Account page"]
    GD --> AD["Admin console (lazy loaded)"]
    CL --> API["API calls"]
```

Key behaviours:

- The access token is never written to storage. After a page reload the session is restored silently from the refresh cookie.
- Several tabs refreshing at once are handled (Web Locks, plus one retry on `refresh_superseded`).
- A tool page shows "17 of 20 left today" before the user runs anything; the number updates after each run.
- The sign-in `next` parameter accepts same-site paths only (no open redirect).
- Every password field has a **show/hide eye button**.

### 9.2 Loaders

Before, every loading state was a tiny 16 px spinner in the corner and nothing else, so pages looked empty and broken.

```mermaid
flowchart TD
    L["Something is loading"] --> K{"What kind?"}
    K -->|"session restore,<br/>admin code download"| PL["Page loader:<br/>spinning red ring around the Rejecta dot,<br/>label, moving bar, soft blurred colour"]
    K -->|"first load of a page"| SK["Frosted-glass skeleton<br/>shaped like the real page:<br/>stat tiles, table rows, cards, matrix"]
    K -->|"reload with data on screen<br/>(filter, page change, after a save)"| RO["Old content stays, blurred,<br/>not clickable, small 'Updating...' pill"]
    K -->|"model call, 15 to 45 s"| WT["Glass 'working' tile with status line<br/>that changes every few seconds"]
    K -->|"button action"| BS["Small spinner inside the button"]
```

- Nothing is shown for the first 150 ms, so fast loads never flash a loader.
- Skeletons match the size of the real content, so the page does not jump when data arrives.
- Motion stops for people who use "reduce motion". Screen readers get "Loading…" announcements.
- No new library: CSS animations only.

### 9.3 Pagination

| List | How | Where |
|---|---|---|
| Users | page numbers, total count, page size 10 / 25 / 50 / 100, search and filters | backend and frontend (backend existed; frontend now uses the shared control) |
| Partners | same, plus search and status filter | **new** in backend and frontend |
| Audit log | cursor ("load older"), which is the right choice for an append-only log that keeps growing | backend and frontend |
| Plans, Features | not paginated on purpose: both lists are small, and the whole list is needed for dropdowns (a user's plan, the move-users target) | n/a |

The shared control shows "Showing 26–50 of 137", first, previous, numbered pages with gaps (`1 … 4 5 6 … 20`), next, last, and a per-page selector. If a filter makes the list shorter than the current page, it steps back to the last valid page. The backend rejects page 0 and page sizes above 100.

---

## 10. Security controls

| Threat | Control |
|---|---|
| Stolen password database | Argon2id; refresh tokens, reset tokens, partner secrets and recovery codes stored only as hashes; TOTP secrets encrypted |
| Stolen refresh token | rotation; reuse of an old token revokes the whole session and raises an audit alert |
| Brute-force sign-in | IP and per-email limits; database-backed lock after 5 failures; identical error and timing for unknown emails |
| Forged or confused tokens | EdDSA only; token types isolated by header type and audience; claims fully validated; user re-read on every request |
| Cross-site request forgery | SameSite=Strict cookie scoped to the auth path, required `X-Rejecta-Client` header, origin allow-list |
| Cross-site scripting | access token in memory only; strict Content-Security-Policy; no third-party scripts |
| Privilege escalation | first admin only by server script, later admins only by a 2FA-verified admin; role and plan fields rejected on sign-up; role read from the database; admin routes need 2FA |
| Stolen admin password | two-factor authentication, replay-proof codes, limited attempts |
| Leaked database access | Supabase roles have no access to schema `app`; the API role owns nothing and has timeouts; RLS on every table |
| Quota abuse | atomic counters; parallel requests cannot exceed the limit; refunds only for failed work |
| Rogue admin action | every change audited, append-only; admins cannot change their own role or status; last admin protected |
| Misconfigured production | the API refuses to start with unsafe settings; API docs hidden in production |
| Open API from any website | CORS limited to the app and its Netlify previews |

---

## 11. Problems found and fixed on the way

| Problem | Fix |
|---|---|
| Rolling back migration 0001 failed on Supabase (PG17 forbids `DROP OWNED BY` there) | rewritten to revoke grants and drop the role; full downgrade and upgrade tested |
| Tests failed on the second run because old data stayed in the test database | the test setup wipes and rebuilds the database every run; it refuses to touch Supabase |
| Heatmap and Abstract Forge ran the tool automatically when the page opened, and twice in development. With quotas, a page visit would have used credits | they now run only when the user clicks |
| Changing the password would have left the browser with a third-party cookie | that call now goes through the auth proxy |
| After sign-up, the sign-in time was empty | the first session now records it |
| Frontend called port 8000 while the API ran on 8001, so nothing reached it | added a frontend `.env`; the dev proxy now follows the same API setting |
| A first loader was too faint and looked broken | replaced with the ring loader and glass skeletons |
| Stray old text "no account" on the upload page | updated to say it uses one analysis from the plan |

---

## 12. Testing and what is verified

| Check | Result |
|---|---|
| Backend tests | **173 passed**, 3 skipped. They run against a real Postgres 17 container |
| Token attacks | `alg=none`, HS256 key confusion, tampered payload, unknown key, wrong audience, expired tokens, token-type swapping all rejected |
| Concurrency | 20 parallel quota calls against limit 3: exactly 3 allowed |
| Database | 51 of 51 security and integrity checks on Supabase; full downgrade and upgrade works |
| Frontend | TypeScript strict check and production build pass |
| Real browser run (local stack) | sign-up, session restore after reload, tool run with live counter, sign-out, admin first sign-in with QR and recovery codes, plan edit saved to the database and the audit log, all admin pages with no console errors |
| Port fix | proxy and CORS verified with direct requests |

**Honest gaps.** The new page loader, the skeletons, the tool "working" tile, the show/hide eye button and the pagination control were built and compile and the backend side is tested, but they have **not been looked at in a browser by me**. Please open them once. Also not done: a real run against the deployed Railway and Netlify, because nothing is deployed yet.

---

## 13. What happens next

```mermaid
flowchart TD
    A["Reset the Supabase postgres password<br/>(it was shared in a chat)"] --> B["Get the Supabase Session pooler URL<br/>(Railway has no IPv6)"]
    B --> C["Run scripts.db_role against it<br/>to get the API's DATABASE_URL"]
    C --> D["Set Railway variables<br/>ENVIRONMENT=production, keys, URLs"]
    D --> E["Set Netlify variables<br/>and the shared proxy secret"]
    E --> F["Deploy API and frontend TOGETHER"]
    F --> G["Create the first admin<br/>scripts.create_admin"]
    G --> H["Sign in, set up 2FA,<br/>save recovery codes"]
    H --> I["Smoke test: sign-up, one tool run,<br/>usage strip, /health, /docs is 404"]
```

Deploy both sides together: once the new API is live, the tools need sign-in and the old frontend has no sign-in page. The full variable list is in [AUTH_IMPLEMENTATION.md](AUTH_IMPLEMENTATION.md), section 12.

**Open items**

| Item | Owner |
|---|---|
| Reset the Supabase password; keep `.env` private (it holds the database password and the signing keys) | you |
| Commit the branch and open a pull request | you decide when |
| Visual check of the loaders, eye button and pagination | you or me on request |
| Email provider, Google login, per-end-user partner limits | later, by decision |
| Move rate limits and caches to Redis if the API ever runs on more than one instance | later |

---

## 14. Where to find things

| What | Where |
|---|---|
| Detailed engineering log | [AUTH_IMPLEMENTATION.md](AUTH_IMPLEMENTATION.md) |
| Partner developer guide | [PARTNER_INTEGRATION.md](PARTNER_INTEGRATION.md) |
| Database notes | `rejecta-api/db/README.md` |
| Auth code | `rejecta-api/auth/`, `rejecta-api/routers/` |
| Migrations | `rejecta-api/db/migrations/versions/` |
| Admin scripts | `rejecta-api/scripts/` (`create_admin`, `generate_keys`, `db_role`, `verify_db`) |
| Frontend auth and admin | `rejecta-frontend/src/auth/`, `src/admin/`, `src/pages/admin/` |
| Loaders and shared UI | `rejecta-frontend/src/components/Loaders.tsx`, `AdminSkeletons.tsx`, `ui.tsx` |
