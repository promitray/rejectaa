"""Admin API end to end: users, overrides, plans, features, insights (Phase 5)."""

import pytest
from fastapi.testclient import TestClient

from tests.integration.admin_helpers import admin_session, bearer
from tests.integration.helpers import PASSWORD, WEB, new_email

HEATMAP_BODY = {"text": "This is a manuscript excerpt with enough words to analyse. " * 3}


@pytest.fixture
def admin(client, db) -> dict[str, str]:
    email, token = admin_session(client, db)
    return {"email": email, "headers": bearer(token)}


def signup(client: TestClient) -> tuple[str, str, str]:
    email = new_email()
    body = client.post("/auth/signup", headers=WEB, json={"email": email, "password": PASSWORD}).json()
    return email, body["user"]["id"], body["access_token"]


def plan_by_key(client: TestClient, admin: dict, key: str) -> dict:
    return next(p for p in client.get("/admin/plans", headers=admin["headers"]).json() if p["key"] == key)


# --- access -------------------------------------------------------------------


def test_admin_api_requires_admin_with_mfa(client, admin):
    _, _, user_token = signup(client)
    assert client.get("/admin/users").status_code == 401
    assert client.get("/admin/users", headers=bearer(user_token)).json()["code"] == "admin_required"
    assert client.get("/admin/users", headers=admin["headers"]).status_code == 200


# --- users ----------------------------------------------------------------------


def test_list_search_and_filter_users(client, admin):
    email, user_id, _ = signup(client)
    found = client.get("/admin/users", headers=admin["headers"], params={"search": email[:16]}).json()
    assert found["total"] == 1 and found["items"][0]["id"] == user_id
    assert found["items"][0]["plan"]["key"] == "free"
    admins = client.get("/admin/users", headers=admin["headers"], params={"role": "admin", "page_size": 100}).json()
    assert all(u["role"] == "admin" for u in admins["items"])
    assert client.get("/admin/users", headers=admin["headers"], params={"search": "%"}).json()["total"] == 0


def test_create_user_with_setup_link(client, admin):
    email = new_email()
    created = client.post("/admin/users", headers=admin["headers"], json={"email": email, "full_name": "New Person"})
    assert created.status_code == 201
    url = created.json()["password_setup"]["url"]
    token = url.split("token=")[1]
    assert url.startswith("http") and token.startswith("pr_")

    weak = client.post("/auth/reset-password", headers=WEB, json={"token": token, "new_password": "password123"})
    assert weak.status_code == 400 and weak.json()["code"] == "weak_password"
    ok = client.post("/auth/reset-password", headers=WEB, json={"token": token, "new_password": "fresh-start-2026"})
    assert ok.status_code == 204
    again = client.post("/auth/reset-password", headers=WEB, json={"token": token, "new_password": "another-one-2026"})
    assert again.status_code == 400 and again.json()["code"] == "invalid_reset_token"

    login = client.post("/auth/login", headers=WEB, json={"email": email, "password": "fresh-start-2026"})
    assert login.status_code == 200
    dup = client.post("/admin/users", headers=admin["headers"], json={"email": email.upper()})
    assert dup.status_code == 409


def test_reset_link_ends_sessions_and_old_link_dies(client, admin):
    email, user_id, user_token = signup(client)
    first = client.post(f"/admin/users/{user_id}/password-reset-link", headers=admin["headers"]).json()
    second = client.post(f"/admin/users/{user_id}/password-reset-link", headers=admin["headers"]).json()
    old = first["url"].split("token=")[1]
    new = second["url"].split("token=")[1]
    assert client.post("/auth/reset-password", headers=WEB,
                       json={"token": old, "new_password": "brand-new-pass-1"}).status_code == 400
    assert client.post("/auth/reset-password", headers=WEB,
                       json={"token": new, "new_password": "brand-new-pass-1"}).status_code == 204
    assert client.get("/me", headers=bearer(user_token)).status_code == 401


def test_update_user_plan_role_status(client, admin, db):
    email, user_id, user_token = signup(client)
    researcher = plan_by_key(client, admin, "researcher")
    changed = client.patch(f"/admin/users/{user_id}", headers=admin["headers"], json={"plan_id": researcher["id"]})
    assert changed.status_code == 200 and changed.json()["plan"]["key"] == "researcher"
    assert client.get("/me", headers=bearer(user_token)).json()["plan"]["key"] == "researcher"

    suspended = client.patch(f"/admin/users/{user_id}", headers=admin["headers"], json={"status": "suspended"})
    assert suspended.json()["status"] == "suspended"
    assert client.get("/me", headers=bearer(user_token)).status_code == 401

    me_id = client.get("/me", headers=admin["headers"]).json()["id"]
    selfie = client.patch(f"/admin/users/{me_id}", headers=admin["headers"], json={"role": "user"})
    assert selfie.status_code == 409 and selfie.json()["code"] == "self_change"
    assert client.patch(f"/admin/users/{user_id}", headers=admin["headers"], json={"role": "superuser"}).status_code == 422
    assert client.patch(f"/admin/users/{user_id}", headers=admin["headers"], json={}).status_code == 400


def test_user_detail_override_and_usage_reset(client, admin):
    email, user_id, user_token = signup(client)
    put = client.put(f"/admin/users/{user_id}/overrides/ai_heatmap", headers=admin["headers"],
                     json={"daily_limit_set": True, "daily_limit": 1, "reason": "trial"})
    assert put.status_code == 200 and put.json()["daily_limit"] == 1

    assert client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY).status_code == 200
    assert client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY).status_code == 429

    detail = client.get(f"/admin/users/{user_id}", headers=admin["headers"]).json()
    heat = next(f for f in detail["features"] if f["key"] == "ai_heatmap")
    assert heat["used_today"] == 1 and heat["daily_limit"] == 1
    assert detail["overrides"][0]["reason"] == "trial" and detail["active_sessions"] >= 1

    assert client.post(f"/admin/users/{user_id}/usage/reset", headers=admin["headers"],
                       json={"feature_key": "ai_heatmap"}).status_code == 204
    assert client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY).status_code == 200

    assert client.delete(f"/admin/users/{user_id}/overrides/ai_heatmap", headers=admin["headers"]).status_code == 204
    assert client.delete(f"/admin/users/{user_id}/overrides/ai_heatmap", headers=admin["headers"]).status_code == 404
    bad = client.put(f"/admin/users/{user_id}/overrides/ai_heatmap", headers=admin["headers"],
                     json={"daily_limit": 5})
    assert bad.status_code == 422
    assert client.put(f"/admin/users/{user_id}/overrides/nope", headers=admin["headers"],
                      json={"enabled": True}).status_code == 404


def test_revoke_sessions_and_mfa_reset(client, admin, db):
    _, user_id, user_token = signup(client)
    assert client.post(f"/admin/users/{user_id}/revoke-sessions", headers=admin["headers"]).status_code == 204
    assert client.get("/me", headers=bearer(user_token)).status_code == 401

    other_email, other_token = admin_session(client, db)
    other_id = client.get("/me", headers=bearer(other_token)).json()["id"]
    assert client.post(f"/admin/users/{other_id}/mfa/reset", headers=admin["headers"]).status_code == 204
    assert client.get("/me", headers=bearer(other_token)).status_code == 401
    challenge = client.post("/auth/login", headers=WEB, json={"email": other_email, "password": PASSWORD}).json()
    assert challenge["mfa_enrollment_required"] is True


# --- plans & features -------------------------------------------------------------


def test_plan_lifecycle(client, admin):
    headers = admin["headers"]
    free = plan_by_key(client, admin, "free")
    assert free["is_default"] is True and len(free["features"]) == 5

    created = client.post("/admin/plans", headers=headers,
                          json={"key": "pilot-x", "name": "Pilot X", "copy_from_plan_id": free["id"]})
    assert created.status_code == 201
    plan = created.json()
    assert [f["daily_limit"] for f in plan["features"]] == [f["daily_limit"] for f in free["features"]]
    assert client.post("/admin/plans", headers=headers, json={"key": "pilot-x", "name": "Again"}).status_code == 409

    matrix = [{"feature_key": f["feature_key"], "enabled": True, "daily_limit": 7, "monthly_limit": 70}
              for f in plan["features"]]
    saved = client.put(f"/admin/plans/{plan['id']}/features", headers=headers,
                       json={"version": plan["version"], "features": matrix})
    assert saved.status_code == 200
    assert all(f["daily_limit"] == 7 for f in saved.json()["features"])
    stale = client.put(f"/admin/plans/{plan['id']}/features", headers=headers,
                       json={"version": plan["version"], "features": matrix})
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"

    version = saved.json()["version"]
    bad = client.put(f"/admin/plans/{plan['id']}/features", headers=headers, json={"version": version, "features": [
        {"feature_key": "ai_heatmap", "enabled": True, "daily_limit": 10, "monthly_limit": 5}]})
    assert bad.status_code == 422
    unknown = client.put(f"/admin/plans/{plan['id']}/features", headers=headers, json={"version": version, "features": [
        {"feature_key": "teleport", "enabled": True}]})
    assert unknown.status_code == 400 and unknown.json()["code"] == "unknown_feature"

    renamed = client.patch(f"/admin/plans/{plan['id']}", headers=headers, json={"version": version, "name": "Pilot Y"})
    assert renamed.json()["name"] == "Pilot Y"

    dup = client.post(f"/admin/plans/{plan['id']}/duplicate", headers=headers, json={"key": "pilot-z", "name": "Z"})
    assert dup.status_code == 201 and dup.json()["features"][0]["daily_limit"] == 7


def test_users_feel_plan_changes_immediately(client, admin):
    headers = admin["headers"]
    _, user_id, user_token = signup(client)
    plan = client.post("/admin/plans", headers=headers, json={"key": "tight", "name": "Tight"}).json()
    assert client.patch(f"/admin/users/{user_id}", headers=headers, json={"plan_id": plan["id"]}).status_code == 200

    denied = client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY)
    assert denied.status_code == 403 and denied.json()["code"] == "feature_not_in_plan"

    client.put(f"/admin/plans/{plan['id']}/features", headers=headers, json={"version": plan["version"], "features": [
        {"feature_key": "ai_heatmap", "enabled": True, "daily_limit": 1}]})
    assert client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY).status_code == 200
    assert client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY).status_code == 429

    usage = client.get(f"/admin/plans/{plan['id']}/usage", headers=headers, params={"period": "day"}).json()
    heat = next(r for r in usage["rows"] if r["feature_key"] == "ai_heatmap")
    assert heat["used"] == 1 and heat["subjects"] == 1


def test_default_switch_archive_and_migrate(client, admin):
    headers = admin["headers"]
    free = plan_by_key(client, admin, "free")
    temp = client.post("/admin/plans", headers=headers, json={"key": "temp-default", "name": "Temp"}).json()
    try:
        switched = client.patch(f"/admin/plans/{temp['id']}", headers=headers,
                                json={"version": temp["version"], "is_default": True})
        assert switched.status_code == 200 and switched.json()["is_default"] is True
        assert plan_by_key(client, admin, "free")["is_default"] is False
        archive_default = client.patch(f"/admin/plans/{temp['id']}", headers=headers,
                                       json={"version": switched.json()["version"], "status": "archived"})
        assert archive_default.status_code == 409
    finally:
        free = plan_by_key(client, admin, "free")
        client.patch(f"/admin/plans/{free['id']}", headers=headers, json={"version": free["version"], "is_default": True})
    assert plan_by_key(client, admin, "free")["is_default"] is True

    _, user_id, _ = signup(client)
    client.patch(f"/admin/users/{user_id}", headers=headers, json={"plan_id": temp["id"]})
    temp = plan_by_key(client, admin, "temp-default")
    blocked = client.patch(f"/admin/plans/{temp['id']}", headers=headers,
                           json={"version": temp["version"], "status": "archived"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "rule_violation"

    moved = client.post(f"/admin/plans/{temp['id']}/migrate-users", headers=headers, json={"to_plan_id": free["id"]})
    assert moved.json()["moved_users"] == 1
    temp = plan_by_key(client, admin, "temp-default")
    archived = client.patch(f"/admin/plans/{temp['id']}", headers=headers,
                            json={"version": temp["version"], "status": "archived"})
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    to_archived = client.patch(f"/admin/users/{user_id}", headers=headers, json={"plan_id": temp["id"]})
    assert to_archived.status_code == 400 and to_archived.json()["code"] == "plan_archived"


def test_feature_kill_switch(client, admin):
    headers = admin["headers"]
    _, _, user_token = signup(client)
    try:
        off = client.patch("/admin/features/ai_heatmap", headers=headers, json={"globally_enabled": False})
        assert off.status_code == 200 and off.json()["globally_enabled"] is False
        denied = client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY)
        assert denied.json()["code"] == "feature_disabled"
    finally:
        client.patch("/admin/features/ai_heatmap", headers=headers, json={"globally_enabled": True})
    assert client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY).status_code == 200
    assert client.patch("/admin/features/nope", headers=headers, json={"name": "x"}).status_code == 404


# --- insights -------------------------------------------------------------------------


def test_stats_usage_and_audit(client, admin):
    headers = admin["headers"]
    _, user_id, user_token = signup(client)
    client.post("/ai-signal-heatmap/analyze", headers=bearer(user_token), json=HEATMAP_BODY)
    client.patch(f"/admin/users/{user_id}", headers=headers, json={"full_name": "Audited"})

    stats = client.get("/admin/stats", headers=headers).json()
    assert stats["users_total"] >= 2 and stats["admins"] >= 1 and stats["usage_today"] >= 1

    usage = client.get("/admin/usage", headers=headers, params={"days": 7}).json()
    assert any(p["feature_key"] == "ai_heatmap" and p["committed"] >= 1 for p in usage["series"])

    page = client.get("/admin/audit-log", headers=headers, params={"action": "admin.user_updated", "limit": 5}).json()
    entry = next(e for e in page["items"] if e["target_id"] == user_id)
    assert entry["actor_email"] == admin["email"] and entry["after"] == {"full_name": "Audited"}
