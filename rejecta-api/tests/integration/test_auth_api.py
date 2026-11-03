"""End-to-end auth flows through the real app and a real Postgres."""

import pytest
from fastapi.testclient import TestClient

from auth.sessions import hash_refresh_token
from config import settings
from tests.integration.helpers import COOKIE, PASSWORD, WEB, new_email


def signup(client: TestClient, email: str | None = None, password: str = PASSWORD, **extra):
    return client.post("/auth/signup", headers=WEB, json={"email": email or new_email(), "password": password, **extra})


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/auth/login", headers=WEB, json={"email": email, "password": password})


def refresh_with(client: TestClient, refresh_token: str):
    """Send exactly this refresh token, ignoring whatever is in the cookie jar."""
    client.cookies.clear()
    response = client.post("/auth/refresh", headers={**WEB, "Cookie": f"{COOKIE}={refresh_token}"})
    client.cookies.clear()
    return response


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def me(client: TestClient, token: str):
    return client.get("/me", headers=bearer(token))


# --- signup ---------------------------------------------------------------


def test_signup_signs_in_with_default_plan(client):
    email = new_email()
    response = signup(client, email.upper(), full_name="  Ada Lovelace ")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == settings.ACCESS_TOKEN_TTL_MINUTES * 60
    assert body["user"]["email"] == email and body["user"]["role"] == "user"
    assert body["user"]["full_name"] == "Ada Lovelace"
    assert "password" not in response.text.lower()

    cookie = response.headers["set-cookie"]
    for attribute in ("HttpOnly", "SameSite=strict", "Path=/auth"):
        assert attribute.lower() in cookie.lower()
    assert response.headers["cache-control"] == "no-store"

    profile = me(client, body["access_token"])
    assert profile.status_code == 200
    assert profile.json()["plan"]["key"] == "free"
    assert profile.json()["aal"] == 1


def test_signup_cannot_choose_role_or_plan(client):
    assert signup(client, role="admin").status_code == 422
    assert signup(client, plan_id="00000000-0000-0000-0000-000000000000").status_code == 422


def test_signup_duplicate_email_any_case(client):
    email = new_email()
    assert signup(client, email).status_code == 201
    response = signup(client, email.upper())
    assert response.status_code == 409 and response.json()["code"] == "email_taken"


def test_signup_weak_password(client):
    response = signup(client, password="password123")
    assert response.status_code == 400 and response.json()["code"] == "weak_password"


def test_signup_rate_limited_per_ip(client):
    for _ in range(5):
        assert signup(client).status_code == 201
    response = signup(client)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_cookie_routes_require_client_header_and_allowed_origin(client):
    body = {"email": new_email(), "password": PASSWORD}
    assert client.post("/auth/signup", json=body).json()["code"] == "csrf_check_failed"
    evil = client.post("/auth/signup", json=body, headers={**WEB, "Origin": "https://evil.example"})
    assert evil.status_code == 403 and evil.json()["code"] == "csrf_check_failed"
    ok = client.post("/auth/signup", json=body, headers={**WEB, "Origin": "https://rejecta.scivalon.com"})
    assert ok.status_code == 201


# --- login ----------------------------------------------------------------


def test_login_and_generic_errors(client):
    email = new_email()
    signup(client, email)
    assert login(client, email).status_code == 200
    wrong = login(client, email, "not-the-password")
    unknown = login(client, new_email())
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Invalid email or password.", "code": "invalid_credentials"}


def test_lockout_after_repeated_failures(client, db):
    email = new_email()
    signup(client, email)
    for _ in range(settings.LOGIN_MAX_FAILURES):
        assert login(client, email, "wrong-password-x").status_code == 401
    blocked = login(client, email)  # even the right password
    assert blocked.status_code == 429

    # Persistent lock survives a process restart (in-memory limiter cleared).
    from auth import limits

    limits.reset_all()
    assert login(client, email).status_code == 429
    locked = db(lambda c: c.fetchval("SELECT locked_until > now() FROM users WHERE email = $1", email))
    assert locked is True


def test_suspended_user_is_signed_out_and_cannot_log_in(client, db):
    email = new_email()
    token = signup(client, email).json()["access_token"]
    db(lambda c: c.execute("UPDATE users SET status = 'suspended' WHERE email = $1", email))
    assert me(client, token).status_code == 401
    response = login(client, email)
    assert response.status_code == 403 and response.json()["code"] == "account_suspended"


# --- refresh / sessions ---------------------------------------------------


def test_refresh_rotates_and_detects_reuse(client, db):
    first = signup(client)
    token_a = first.cookies[COOKIE]

    rotated = refresh_with(client, token_a)
    assert rotated.status_code == 200
    token_b = rotated.cookies[COOKIE]
    assert token_b != token_a
    assert me(client, rotated.json()["access_token"]).status_code == 200

    # Same token again within the grace window: "superseded", nothing issued.
    again = refresh_with(client, token_a)
    assert again.status_code == 401 and again.json()["code"] == "refresh_superseded"
    assert COOKIE not in again.cookies

    # After the grace window, reuse means theft: the whole session dies.
    db(lambda c: c.execute(
        "UPDATE refresh_tokens SET revoked_at = now() - interval '1 minute' WHERE token_hash = $1",
        hash_refresh_token(token_a),
    ))
    stolen = refresh_with(client, token_a)
    assert stolen.status_code == 401 and stolen.json()["code"] == "invalid_refresh_token"
    assert refresh_with(client, token_b).status_code == 401
    reason_b = db(lambda c: c.fetchval(
        "SELECT revoked_reason FROM refresh_tokens WHERE token_hash = $1", hash_refresh_token(token_b)
    ))
    assert reason_b == "reuse_detected"
    audited = db(lambda c: c.fetchval("SELECT count(*) FROM audit_log WHERE action = 'auth.refresh_reuse_detected'"))
    assert audited >= 1


def test_refresh_without_or_with_garbage_cookie(client):
    assert client.post("/auth/refresh", headers=WEB).json()["code"] == "missing_refresh_token"
    assert refresh_with(client, "rt_not-a-real-token").json()["code"] == "invalid_refresh_token"


def test_session_has_absolute_lifetime(client, db):
    response = signup(client)
    token = response.cookies[COOKIE]
    days = settings.SESSION_MAX_LIFETIME_DAYS + 1
    db(lambda c: c.execute(
        "UPDATE refresh_tokens SET session_started_at = now() - make_interval(days => $1), "
        "created_at = now() - make_interval(days => $1) WHERE token_hash = $2",
        days,
        hash_refresh_token(token),
    ))
    assert refresh_with(client, token).status_code == 401


def test_logout_ends_session(client):
    response = signup(client)
    token = response.cookies[COOKIE]
    client.cookies.clear()
    out = client.post("/auth/logout", headers={**WEB, "Cookie": f"{COOKIE}={token}"})
    assert out.status_code == 204
    assert 'max-age=0' in out.headers["set-cookie"].lower() or "expires=" in out.headers["set-cookie"].lower()
    assert refresh_with(client, token).status_code == 401


def test_logout_all_kills_access_and_refresh_tokens(client):
    email = new_email()
    first = signup(client, email)
    second = login(client, email)
    access = second.json()["access_token"]
    assert client.post("/auth/logout-all", headers=bearer(access)).status_code == 204
    assert me(client, access).status_code == 401
    assert me(client, first.json()["access_token"]).status_code == 401
    assert refresh_with(client, first.cookies[COOKIE]).status_code == 401
    assert refresh_with(client, second.cookies[COOKIE]).status_code == 401


# --- password change ------------------------------------------------------


def test_change_password(client):
    email = new_email()
    old = signup(client, email)
    other_device = login(client, email)
    old_access = old.json()["access_token"]

    wrong = client.post("/auth/change-password", headers=bearer(old_access),
                        json={"current_password": "nope-nope-nope", "new_password": "a-new-passphrase"})
    assert wrong.status_code == 400 and wrong.json()["code"] == "invalid_current_password"
    same = client.post("/auth/change-password", headers=bearer(old_access),
                       json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert same.status_code == 400

    changed = client.post("/auth/change-password", headers=bearer(old_access),
                          json={"current_password": PASSWORD, "new_password": "a-new-passphrase"})
    assert changed.status_code == 200, changed.text
    assert me(client, changed.json()["access_token"]).status_code == 200
    assert refresh_with(client, changed.cookies[COOKIE]).status_code == 200

    assert me(client, old_access).status_code == 401
    assert refresh_with(client, other_device.cookies[COOKIE]).status_code == 401
    assert login(client, email).status_code == 401
    assert login(client, email, "a-new-passphrase").status_code == 200


# --- tokens ---------------------------------------------------------------


@pytest.mark.parametrize("header", [None, "Bearer", "Bearer abc.def.ghi", "Basic dXNlcjpwYXNz"])
def test_me_rejects_missing_or_bad_tokens(client, header):
    response = client.get("/me", headers={"Authorization": header} if header else {})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_token_for_deleted_user_rejected(client, db):
    email = new_email()
    token = signup(client, email).json()["access_token"]
    db(lambda c: c.execute("DELETE FROM users WHERE email = $1", email))
    assert me(client, token).status_code == 401


def test_jwks_endpoint(client):
    response = client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    (key,) = response.json()["keys"]
    assert key["kid"] == settings.JWT_KEY_ID and "d" not in key


def test_audit_trail(client, db):
    email = new_email()
    signup(client, email)
    login(client, email)
    login(client, email, "wrong-password-x")
    actions = db(lambda c: c.fetch(
        "SELECT a.action FROM audit_log a JOIN users u ON u.id::text = a.target_id WHERE u.email = $1", email
    ))
    assert {r["action"] for r in actions} >= {"auth.signup", "auth.login", "auth.login_failed"}


def test_existing_endpoints_still_public(client):
    assert client.get("/health").json()["database"] == "ok"
    assert client.post("/citations", json={"references": []}).status_code == 400


def test_refresh_token_stored_only_as_sha256(client, db):
    token = signup(client).cookies[COOKIE]
    by_hash = db(lambda c: c.fetchval(
        "SELECT count(*) FROM refresh_tokens WHERE token_hash = $1", hash_refresh_token(token)
    ))
    by_raw = db(lambda c: c.fetchval(
        "SELECT count(*) FROM refresh_tokens WHERE token_hash = convert_to($1, 'UTF8')", token
    ))
    assert (by_hash, by_raw) == (1, 0)


def test_logout_never_ends_a_different_account_signed_in_the_same_browser(client):
    """The refresh cookie is shared by all tabs; a tab may only end its own sign-in."""
    first = signup(client)  # account A in "tab 1"
    second = signup(client)  # account B in "tab 2" replaces the shared cookie
    a_access = first.json()["access_token"]
    b_cookie = second.cookies[COOKIE]

    client.cookies.clear()
    out = client.post("/auth/logout", headers={**WEB, **bearer(a_access), "Cookie": f"{COOKIE}={b_cookie}"})
    assert out.status_code == 204
    assert "set-cookie" not in out.headers  # B's cookie is left alone
    assert refresh_with(client, b_cookie).status_code == 200  # B is still signed in

    # A tab logging out of its own sign-in still ends it.
    own = signup(client)
    own_cookie = own.cookies[COOKIE]
    client.cookies.clear()
    ended = client.post("/auth/logout", headers={**WEB, **bearer(own.json()["access_token"]), "Cookie": f"{COOKIE}={own_cookie}"})
    assert ended.status_code == 204
    assert refresh_with(client, own_cookie).status_code == 401
