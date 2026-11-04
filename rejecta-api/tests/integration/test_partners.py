"""Partner integrations (ChemLabFlow-style server-to-server access), Phase 8."""

import base64

import pytest
from fastapi.testclient import TestClient

from tests.integration.admin_helpers import admin_session, bearer

HEATMAP = "/ai-signal-heatmap/analyze"
HEATMAP_BODY = {"text": "This is a manuscript excerpt with enough words to analyse. " * 3}


@pytest.fixture
def admin(client, db) -> dict:
    _, token = admin_session(client, db)
    return bearer(token)


def plan_id(client: TestClient, admin: dict, key: str) -> str:
    return next(p["id"] for p in client.get("/admin/plans", headers=admin).json() if p["key"] == key)


def create_partner(client, admin, scopes=("ai_heatmap",), plan="partner") -> dict:
    response = client.post("/admin/partners", headers=admin, json={
        "name": "ChemLabFlow", "plan_id": plan_id(client, admin, plan), "scopes": list(scopes)})
    assert response.status_code == 201, response.text
    return response.json()


def get_token(client, client_id, secret, scope=None, basic=False):
    data = {"grant_type": "client_credentials"}
    headers = {}
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    else:
        data.update(client_id=client_id, client_secret=secret)
    if scope:
        data["scope"] = scope
    return client.post("/oauth/token", data=data, headers=headers)


def test_full_partner_flow(client, admin):
    created = create_partner(client, admin)
    creds = created["credentials"]
    assert creds["client_id"].startswith("rjc_") and creds["client_secret"].startswith("rjs_")
    assert creds["secret"]["last4"] == creds["client_secret"][-4:]

    issued = get_token(client, creds["client_id"], creds["client_secret"])
    assert issued.status_code == 200 and issued.headers["cache-control"] == "no-store"
    body = issued.json()
    assert body["token_type"] == "Bearer" and body["scope"] == "ai_heatmap" and body["expires_in"] == 900

    work = client.post(HEATMAP, headers=bearer(body["access_token"]), json=HEATMAP_BODY)
    assert work.status_code == 200

    detail = client.get(f"/admin/partners/{created['partner']['id']}", headers=admin).json()
    heat = next(f for f in detail["features"] if f["key"] == "ai_heatmap")
    assert heat["used_today"] == 1
    assert get_token(client, creds["client_id"], creds["client_secret"], basic=True).status_code == 200


def test_partner_token_is_limited_to_tools_and_scopes(client, admin):
    creds = create_partner(client, admin, scopes=("ai_heatmap",))["credentials"]
    token = get_token(client, creds["client_id"], creds["client_secret"]).json()["access_token"]
    assert client.get("/me", headers=bearer(token)).status_code == 401
    assert client.get("/admin/users", headers=bearer(token)).status_code == 401
    out_of_scope = client.post("/rebuttal-writer/analyze", headers=bearer(token),
                               json={"reviewer_report_text": "Reviewer 1: the method section is unclear."})
    assert out_of_scope.status_code == 403 and out_of_scope.json()["code"] == "insufficient_scope"
    assert get_token(client, creds["client_id"], creds["client_secret"], scope="rebuttal_writer").json()["error"] \
        == "invalid_scope"


def test_bad_credentials_and_grant(client, admin):
    creds = create_partner(client, admin)["credentials"]
    wrong = get_token(client, creds["client_id"], "rjs_wrong")
    assert wrong.status_code == 401 and wrong.json()["error"] == "invalid_client"
    other = get_token(client, "rjc_doesnotexist000000", creds["client_secret"])
    assert other.status_code == 401
    grant = client.post("/oauth/token", data={"grant_type": "password", "client_id": "x", "client_secret": "y"})
    assert grant.json()["error"] == "unsupported_grant_type"


def test_failed_client_auth_is_rate_limited(client, admin):
    creds = create_partner(client, admin)["credentials"]
    statuses = [get_token(client, creds["client_id"], "rjs_wrong").status_code for _ in range(11)]
    assert statuses[:10] == [401] * 10 and statuses[10] == 429


def test_secret_rotation_and_revocation(client, admin):
    created = create_partner(client, admin)
    partner_id = created["partner"]["id"]
    old = created["credentials"]
    old_token = get_token(client, old["client_id"], old["client_secret"]).json()["access_token"]

    new = client.post(f"/admin/partners/{partner_id}/secrets", headers=admin).json()
    assert client.post(f"/admin/partners/{partner_id}/secrets", headers=admin).json()["code"] == "too_many_secrets"
    assert get_token(client, new["client_id"], new["client_secret"]).status_code == 200

    revoke = client.delete(f"/admin/partners/{partner_id}/secrets/{old['secret']['id']}", headers=admin)
    assert revoke.status_code == 204
    assert get_token(client, old["client_id"], old["client_secret"]).status_code == 401
    assert client.post(HEATMAP, headers=bearer(old_token), json=HEATMAP_BODY).status_code == 401  # token revoked
    fresh = get_token(client, new["client_id"], new["client_secret"]).json()["access_token"]
    assert client.post(HEATMAP, headers=bearer(fresh), json=HEATMAP_BODY).status_code == 200


def test_disable_partner_and_plan_limits(client, admin, db):
    created = create_partner(client, admin, plan="free")
    partner_id = created["partner"]["id"]
    creds = created["credentials"]
    token = get_token(client, creds["client_id"], creds["client_secret"]).json()["access_token"]
    db(lambda c: c.execute(
        "UPDATE usage_counters SET count = 20 WHERE subject_type = 'client' AND subject_id = $1", partner_id))
    client.post(HEATMAP, headers=bearer(token), json=HEATMAP_BODY)  # creates counters if missing
    db(lambda c: c.execute(
        "UPDATE usage_counters SET count = 20 WHERE subject_type = 'client' AND subject_id = $1 AND period = 'day'",
        partner_id))
    limited = client.post(HEATMAP, headers=bearer(token), json=HEATMAP_BODY)
    assert limited.status_code == 429 and limited.json()["code"] == "quota_exceeded"

    disabled = client.patch(f"/admin/partners/{partner_id}", headers=admin, json={"status": "disabled"})
    assert disabled.json()["status"] == "disabled"
    assert client.post(HEATMAP, headers=bearer(token), json=HEATMAP_BODY).status_code == 401
    assert get_token(client, creds["client_id"], creds["client_secret"]).status_code == 401


def test_partner_validation(client, admin):
    bad_scope = client.post("/admin/partners", headers=admin, json={
        "name": "X", "plan_id": plan_id(client, admin, "partner"), "scopes": ["teleport"]})
    assert bad_scope.status_code == 400 and bad_scope.json()["code"] == "unknown_feature"
    no_scope = client.post("/admin/partners", headers=admin, json={
        "name": "X", "plan_id": plan_id(client, admin, "partner"), "scopes": []})
    assert no_scope.status_code == 422
    listed = client.get("/admin/partners", headers=admin).json()
    assert listed["total"] >= 1 and listed["items"]
    assert all("client_secret" not in str(p) for p in listed["items"])
