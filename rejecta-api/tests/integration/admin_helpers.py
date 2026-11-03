"""Create admins and get a two-factor (aal=2) admin session in tests."""

import time

from fastapi.testclient import TestClient

from auth import mfa
from auth.passwords import hash_password
from tests.integration.helpers import PASSWORD, WEB, new_email


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


def admin_session(client: TestClient, db) -> tuple[str, str]:
    """(email, access_token) for a freshly enrolled admin with aal=2."""
    email = make_admin(db)
    token = client.post("/auth/login", headers=WEB, json={"email": email, "password": PASSWORD}).json()["mfa_token"]
    secret = client.post("/auth/mfa/enroll/start", headers=WEB, json={"mfa_token": token}).json()["secret"]
    code = mfa.code_for_step(secret, int(time.time() // 30))
    body = client.post("/auth/mfa/enroll/confirm", headers=WEB, json={"mfa_token": token, "code": code}).json()
    return email, body["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
