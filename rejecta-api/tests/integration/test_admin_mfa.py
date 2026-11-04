"""Admin two-factor authentication, end to end."""

import time

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from auth import mfa
from auth.dependencies import CurrentUser, require_admin
from auth.passwords import hash_password
from auth.tokens import issue_access_token
from db.repos import mfa as mfa_repo
from tests.integration.helpers import COOKIE, PASSWORD, WEB, new_email

PROBE = "/__test/admin-only"


@pytest.fixture(scope="module", autouse=True)
def admin_probe(client: TestClient) -> None:
    """A throwaway admin-only route, to exercise require_admin."""
    app = client.app
    if not any(getattr(route, "path", None) == PROBE for route in app.routes):

        @app.get(PROBE)
        async def probe(current: CurrentUser = Depends(require_admin)) -> dict[str, str]:
            return {"admin": current.email}


def make_admin(db, email: str | None = None) -> str:
    email = email or new_email()

    async def go(conn):
        await conn.execute(
            "INSERT INTO users (email, password_hash, role) VALUES ($1, $2, 'admin')",
            email,
            await hash_password(PASSWORD),
        )

    db(go)
    return email


def login(client: TestClient, email: str):
    return client.post("/auth/login", headers=WEB, json={"email": email, "password": PASSWORD})


def current_step() -> int:
    return int(time.time() // 30)


def enroll(client: TestClient, email: str) -> dict:
    challenge = login(client, email).json()
    secret = client.post("/auth/mfa/enroll/start", headers=WEB, json={"mfa_token": challenge["mfa_token"]}).json()[
        "secret"
    ]
    step = current_step()
    confirmed = client.post(
        "/auth/mfa/enroll/confirm",
        headers=WEB,
        json={"mfa_token": challenge["mfa_token"], "code": mfa.code_for_step(secret, step)},
    )
    assert confirmed.status_code == 200, confirmed.text
    return {"secret": secret, "step": step, "body": confirmed.json(), "response": confirmed}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_admin_password_alone_gives_no_session(client, db):
    email = make_admin(db)
    response = login(client, email)
    assert response.status_code == 200
    body = response.json()
    assert body["mfa_required"] is True and body["mfa_enrollment_required"] is True
    assert "access_token" not in body
    assert "set-cookie" not in response.headers


def test_enrollment_signs_in_with_aal2_and_recovery_codes(client, db):
    email = make_admin(db)
    challenge = login(client, email).json()
    token = challenge["mfa_token"]

    early = client.post("/auth/mfa/enroll/confirm", headers=WEB, json={"mfa_token": token, "code": "123456"})
    assert early.json()["code"] == "mfa_enrollment_not_started"

    start = client.post("/auth/mfa/enroll/start", headers=WEB, json={"mfa_token": token})
    assert start.status_code == 200
    data = start.json()
    assert data["otpauth_uri"].startswith("otpauth://totp/") and data["account"] == email
    secret = data["secret"]

    wrong = client.post("/auth/mfa/enroll/confirm", headers=WEB, json={"mfa_token": token, "code": "000000"})
    if mfa.match_step(secret, "000000") is None:
        assert wrong.status_code == 401 and wrong.json()["code"] == "invalid_mfa_code"

    good = client.post(
        "/auth/mfa/enroll/confirm",
        headers=WEB,
        json={"mfa_token": token, "code": mfa.code_for_step(secret, current_step())},
    )
    assert good.status_code == 200, good.text
    body = good.json()
    assert len(body["recovery_codes"]) == 10
    assert COOKIE in good.cookies

    me = client.get("/me", headers=bearer(body["access_token"])).json()
    assert me["aal"] == 2 and me["mfa_enabled"] is True and me["role"] == "admin"
    assert client.get(PROBE, headers=bearer(body["access_token"])).status_code == 200

    # The enrollment token is single-use, and a second enrollment is refused.
    again = client.post("/auth/mfa/enroll/start", headers=WEB, json={"mfa_token": token})
    assert again.status_code == 401
    next_login = login(client, email).json()
    assert next_login["mfa_required"] is True and next_login["mfa_enrollment_required"] is False

    # The secret is stored encrypted, never in clear.
    stored = db(lambda c: c.fetchval("SELECT mfa_secret_enc FROM users WHERE email = $1", email))
    assert secret not in stored and mfa.decrypt_secret(stored) == secret


def test_verify_with_totp_and_replay_protection(client, db):
    email = make_admin(db)
    enrolled = enroll(client, email)
    secret, step = enrolled["secret"], enrolled["step"]

    token = login(client, email).json()["mfa_token"]
    replay = client.post("/auth/mfa/verify", headers=WEB,
                         json={"mfa_token": token, "code": mfa.code_for_step(secret, step)})
    assert replay.status_code == 401 and replay.json()["code"] == "invalid_mfa_code"

    db(lambda c: c.execute("UPDATE users SET mfa_last_step = $2 WHERE email = $1", email, step - 5))
    code = mfa.code_for_step(secret, current_step())
    ok = client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": token, "code": code})
    assert ok.status_code == 200, ok.text
    assert client.get("/me", headers=bearer(ok.json()["access_token"])).json()["aal"] == 2

    # The MFA token is single-use; the same code cannot open a second session.
    assert client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": token, "code": code}).status_code == 401
    token2 = login(client, email).json()["mfa_token"]
    assert client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": token2, "code": code}).status_code == 401


def test_refreshed_admin_session_keeps_aal2(client, db):
    email = make_admin(db)
    enrolled = enroll(client, email)
    refresh_token = enrolled["response"].cookies[COOKIE]
    client.cookies.clear()
    refreshed = client.post("/auth/refresh", headers={**WEB, "Cookie": f"{COOKIE}={refresh_token}"})
    client.cookies.clear()
    assert refreshed.status_code == 200
    assert client.get(PROBE, headers=bearer(refreshed.json()["access_token"])).status_code == 200


def test_recovery_codes_are_single_use(client, db):
    email = make_admin(db)
    codes = enroll(client, email)["body"]["recovery_codes"]

    token = login(client, email).json()["mfa_token"]
    used = client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": token, "recovery_code": codes[0].lower()})
    assert used.status_code == 200
    token = login(client, email).json()["mfa_token"]
    again = client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": token, "recovery_code": codes[0]})
    assert again.status_code == 401
    left = db(lambda c: c.fetchval(
        "SELECT count(*) FROM mfa_recovery_codes r JOIN users u ON u.id = r.user_id "
        "WHERE u.email = $1 AND r.used_at IS NULL", email))
    assert left == 9


def test_regenerate_recovery_codes(client, db):
    email = make_admin(db)
    enrolled = enroll(client, email)
    old_codes = enrolled["body"]["recovery_codes"]
    access = enrolled["body"]["access_token"]
    new = client.post("/auth/mfa/recovery-codes", headers=bearer(access))
    assert new.status_code == 200 and len(new.json()["recovery_codes"]) == 10
    token = login(client, email).json()["mfa_token"]
    assert client.post("/auth/mfa/verify", headers=WEB,
                       json={"mfa_token": token, "recovery_code": old_codes[1]}).status_code == 401
    assert client.post("/auth/mfa/verify", headers=WEB,
                       json={"mfa_token": token, "recovery_code": new.json()["recovery_codes"][0]}).status_code == 200


def test_attempts_per_mfa_token_are_limited(client, db):
    email = make_admin(db)
    secret = enroll(client, email)["secret"]
    token = login(client, email).json()["mfa_token"]
    bad = next(f"{n:06d}" for n in range(1_000_000) if mfa.match_step(secret, f"{n:06d}") is None)
    statuses = [
        client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": token, "code": bad}).status_code
        for _ in range(6)
    ]
    assert statuses == [401] * 5 + [429]


def test_token_types_cannot_be_swapped(client, db):
    email = make_admin(db)
    enrolled = enroll(client, email)
    mfa_token = login(client, email).json()["mfa_token"]
    assert client.get("/me", headers=bearer(mfa_token)).status_code == 401
    access = enrolled["body"]["access_token"]
    swapped = client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": access, "code": "123456"})
    assert swapped.status_code == 401 and swapped.json()["code"] == "invalid_mfa_token"
    enroll_with_verify_token = client.post("/auth/mfa/enroll/start", headers=WEB, json={"mfa_token": mfa_token})
    assert enroll_with_verify_token.status_code == 401


def test_mfa_token_dies_when_token_version_changes(client, db):
    email = make_admin(db)
    token = login(client, email).json()["mfa_token"]
    db(lambda c: c.execute("UPDATE users SET token_version = token_version + 1 WHERE email = $1", email))
    response = client.post("/auth/mfa/enroll/start", headers=WEB, json={"mfa_token": token})
    assert response.status_code == 401 and response.json()["code"] == "invalid_mfa_token"


def test_admin_routes_require_admin_role_and_aal2(client, db):
    assert client.get(PROBE).status_code == 401

    user = client.post("/auth/signup", headers=WEB, json={"email": new_email(), "password": PASSWORD}).json()
    denied = client.get(PROBE, headers=bearer(user["access_token"]))
    assert denied.status_code == 403 and denied.json()["code"] == "admin_required"

    email = make_admin(db)
    enroll(client, email)
    row = db(lambda c: c.fetchrow("SELECT id, token_version FROM users WHERE email = $1", email))
    aal1, _ = issue_access_token(user_id=row["id"], role="admin", token_version=row["token_version"],
                                 session_id=row["id"], aal=1)
    weak = client.get(PROBE, headers=bearer(aal1))
    assert weak.status_code == 403 and weak.json()["code"] == "admin_mfa_required"


def test_mfa_reset_signs_out_and_restarts_enrollment(client, db):
    email = make_admin(db)
    access = enroll(client, email)["body"]["access_token"]
    user_id = db(lambda c: c.fetchval("SELECT id FROM users WHERE email = $1", email))
    db(lambda c: mfa_repo.reset(c, user_id))
    assert client.get("/me", headers=bearer(access)).status_code == 401
    assert login(client, email).json()["mfa_enrollment_required"] is True


def test_verify_body_must_have_exactly_one_factor(client, db):
    email = make_admin(db)
    enroll(client, email)
    token = login(client, email).json()["mfa_token"]
    both = client.post("/auth/mfa/verify", headers=WEB,
                       json={"mfa_token": token, "code": "123456", "recovery_code": "ABCDE-FGHJK"})
    neither = client.post("/auth/mfa/verify", headers=WEB, json={"mfa_token": token})
    assert both.status_code == neither.status_code == 422


def test_regular_users_are_not_asked_for_mfa(client):
    email = new_email()
    client.post("/auth/signup", headers=WEB, json={"email": email, "password": PASSWORD})
    body = client.post("/auth/login", headers=WEB, json={"email": email, "password": PASSWORD}).json()
    assert "access_token" in body and "mfa_required" not in body
