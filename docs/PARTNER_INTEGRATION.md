# Rejecta API: partner integration guide

This guide is for engineers at partner platforms (for example ChemLabFlow) who call Rejecta's tools from their own servers.

- **Base URL:** `https://rejecta-production.up.railway.app`
- **Auth:** OAuth 2.0 client credentials (RFC 6749 §4.4), Bearer tokens.
- **Server-to-server only.** Never put the client secret in a browser, a mobile app or a public repository.

---

## 1. Credentials

A Rejecta admin creates your client and sends you:

| Item | Looks like | Notes |
|---|---|---|
| Client ID | `rjc_…` | Not secret, but keep it with the secret. |
| Client secret | `rjs_…` | **Shown once.** Store it in your secret manager. |
| Scopes | `analyse_paper voice_calibrate …` | The tools you may call. |

Your usage is metered as **one pool for your whole platform**. The limits come from the plan the admin assigned to you.

## 2. Get an access token

```bash
curl -X POST https://rejecta-production.up.railway.app/oauth/token \
  -d grant_type=client_credentials \
  -d client_id=$REJECTA_CLIENT_ID \
  -d client_secret=$REJECTA_CLIENT_SECRET
```

```json
{ "access_token": "eyJ…", "token_type": "Bearer", "expires_in": 900, "scope": "analyse_paper ai_heatmap" }
```

- **Basic auth:** HTTP Basic authentication (`-u client_id:client_secret`) is also accepted.
- **Narrower scope:** add `-d "scope=ai_heatmap"` to request a token with fewer scopes. You can only request scopes you were given.
- **Expiry:** tokens last **15 minutes**.
  - Cache the token and reuse it.
  - Request a new one shortly before `expires_in` runs out, or when a call returns `401`.
  - Do not request a token for every call; token requests are rate-limited.

## 3. Call a tool

Send the token as `Authorization: Bearer <token>`.

| Tool | Method and path | Scope |
|---|---|---|
| Full paper analysis | `POST /analyse-paper` (multipart: `file` (PDF or Word .docx, ≤ 10 MB), `journal`, `mode` = `a`\|`b`) | `analyse_paper` |
| Analysis from extracted text | `POST /analyse` (JSON) | `analyse_paper` |
| Voice Calibrator | `POST /voice-calibrate` (JSON) | `voice_calibrate` |
| Abstract Forge | `POST /abstract-forge/generate` (JSON) | `abstract_forge` |
| Rebuttal Writer | `POST /rebuttal-writer/analyze` (JSON) | `rebuttal_writer` |
| AI Signal Heatmap | `POST /ai-signal-heatmap/analyze` (JSON) | `ai_heatmap` |

```bash
curl -X POST https://rejecta-production.up.railway.app/analyse-paper \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@paper.pdf" -F "journal=Nature Machine Intelligence" -F "mode=a"
```

Paper analysis can take 15–45 seconds. Use a client timeout of at least 90 seconds.

### Python

```python
import time, requests

BASE = "https://rejecta-production.up.railway.app"
_token, _expires = None, 0.0

def token() -> str:
    global _token, _expires
    if _token is None or time.time() > _expires - 60:
        r = requests.post(f"{BASE}/oauth/token", timeout=10, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        })
        r.raise_for_status()
        body = r.json()
        _token, _expires = body["access_token"], time.time() + body["expires_in"]
    return _token

def heatmap(text: str) -> dict:
    global _token
    for attempt in range(2):
        r = requests.post(f"{BASE}/ai-signal-heatmap/analyze", timeout=90,
                          headers={"Authorization": f"Bearer {token()}"},
                          json={"text": text, "journal": "Nature"})
        if r.status_code == 401 and attempt == 0:
            _token = None          # token revoked or expired: get a new one once
            continue
        r.raise_for_status()
        return r.json()
```

### Node.js

```js
const BASE = 'https://rejecta-production.up.railway.app'
let cached = { token: null, expiresAt: 0 }

async function getToken() {
  if (cached.token && Date.now() < cached.expiresAt - 60_000) return cached.token
  const res = await fetch(`${BASE}/oauth/token`, {
    method: 'POST',
    body: new URLSearchParams({
      grant_type: 'client_credentials',
      client_id: process.env.REJECTA_CLIENT_ID,
      client_secret: process.env.REJECTA_CLIENT_SECRET,
    }),
  })
  if (!res.ok) throw new Error(`token: ${res.status}`)
  const body = await res.json()
  cached = { token: body.access_token, expiresAt: Date.now() + body.expires_in * 1000 }
  return cached.token
}
```

## 4. Errors

The token endpoint uses the RFC 6749 format: `{"error": "...", "error_description": "..."}`.

| `error` | HTTP | Meaning |
|---|---|---|
| `invalid_client` | 401 | Wrong client ID or secret, the secret was revoked, or the client is disabled. |
| `invalid_scope` | 400 | You asked for a scope you do not have. |
| `unsupported_grant_type` | 400 | Only `client_credentials` is supported. |
| `slow_down` | 429 | Too many token requests or failed attempts. Wait `Retry-After` seconds. |

The tool endpoints use the format `{"detail": "...", "code": "...", ...}`.

| `code` | HTTP | What to do |
|---|---|---|
| `missing_token` / `invalid_token` | 401 | Get a new token and retry once. |
| `insufficient_scope` | 403 | This tool is not in your scopes. Ask the Rejecta admin. |
| `feature_not_in_plan` | 403 | Your plan does not include this tool. |
| `feature_disabled` | 403 | The tool is temporarily switched off for everyone. Retry later. |
| `quota_exceeded` | 429 | Your daily or monthly limit is used up. The body has `period`, `limit`, `used` and `resets_at`, and `Retry-After` is set. |
| (validation) | 400 / 422 | The request is invalid; `detail` explains why. |

**Quota rules:**
- Calls that fail on Rejecta's side (for example a model error) are **not counted**.
- Daily limits reset at 00:00 UTC; monthly limits reset on the 1st (UTC).

## 5. Rotating your secret

You can have **two active secrets** at once, so rotation needs no downtime:

1. Ask the Rejecta admin for a new secret.
2. Deploy it on your side.
3. Ask the admin to revoke the old one.

Revoking a secret also ends every token issued so far. Your client simply requests a new token on the next `401`.

## 6. Security checklist

- **Keep it on the server:** the secret lives only on your servers, in a secret manager.
- **Never log it:** do not log the secret or the access token.
- **Use HTTPS only:** never send credentials over plain HTTP.
- **Report leaks at once:** if a secret may have leaked, tell the Rejecta admin immediately so it can be revoked.
