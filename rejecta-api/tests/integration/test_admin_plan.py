"""Admins start on the Lab plan (unlimited); everyone else on the default plan."""

import uuid

from db.repos import users
from tests.integration.admin_helpers import admin_session, bearer
from tests.integration.helpers import PASSWORD, WEB, new_email


def plan_key(db, email: str) -> str:
    return db(lambda c: c.fetchval(
        "SELECT p.key FROM users u JOIN plans p ON p.id = u.plan_id WHERE u.email = $1", email))


def test_script_and_repo_create_admin_on_lab_plan(client, db):
    email, other = new_email(), new_email()

    async def create(conn):
        await users.create_user(conn, email=email, password_hash="x" * 40, full_name="", role="admin")
        await users.create_user(conn, email=other, password_hash="x" * 40, full_name="", role="user")

    db(create)
    assert plan_key(db, email) == "lab"
    assert plan_key(db, other) == "free"


def test_script_promotion_moves_user_to_lab(client, db):
    email = new_email()
    client.post("/auth/signup", headers=WEB, json={"email": email, "password": PASSWORD})
    assert plan_key(db, email) == "free"
    user_id = db(lambda c: c.fetchval("SELECT id FROM users WHERE email = $1", email))
    db(lambda c: users.promote_to_admin(c, user_id))
    assert plan_key(db, email) == "lab"


def test_admin_api_new_admin_gets_lab_unless_plan_chosen(client, db):
    _, token = admin_session(client, db)
    headers = bearer(token)
    created = client.post("/admin/users", headers=headers, json={"email": new_email(), "role": "admin"}).json()
    assert created["user"]["plan"]["key"] == "lab"

    researcher = next(p["id"] for p in client.get("/admin/plans", headers=headers).json() if p["key"] == "researcher")
    chosen = client.post("/admin/users", headers=headers,
                         json={"email": new_email(), "role": "admin", "plan_id": researcher}).json()
    assert chosen["user"]["plan"]["key"] == "researcher"

    regular = client.post("/admin/users", headers=headers, json={"email": new_email()}).json()
    assert regular["user"]["plan"]["key"] == "free"


def test_promoting_via_admin_api_moves_to_lab_and_demoting_keeps_plan(client, db):
    _, token = admin_session(client, db)
    headers = bearer(token)
    email = new_email()
    user_id = client.post("/auth/signup", headers=WEB, json={"email": email, "password": PASSWORD}).json()["user"]["id"]

    promoted = client.patch(f"/admin/users/{user_id}", headers=headers, json={"role": "admin"})
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin" and promoted.json()["plan"]["key"] == "lab"

    demoted = client.patch(f"/admin/users/{user_id}", headers=headers, json={"role": "user"})
    assert demoted.json()["role"] == "user" and demoted.json()["plan"]["key"] == "lab"  # plan is kept

    other = client.post("/auth/signup", headers=WEB, json={"email": new_email(), "password": PASSWORD}).json()["user"]["id"]
    researcher = next(p["id"] for p in client.get("/admin/plans", headers=headers).json() if p["key"] == "researcher")
    explicit = client.patch(f"/admin/users/{other}", headers=headers, json={"role": "admin", "plan_id": researcher})
    assert explicit.json()["plan"]["key"] == "researcher"


def test_admin_has_no_limits_on_tools(client, db):
    email, token = admin_session(client, db)
    db(lambda c: c.execute(
        "UPDATE users SET plan_id = (SELECT id FROM plans WHERE key = 'lab') WHERE email = $1", email))
    me = client.get("/me", headers=bearer(token)).json()
    assert me["plan"]["key"] == "lab"
    assert all(f["daily_limit"] is None and f["monthly_limit"] is None for f in me["features"])
