"""Feature gating and quotas on the tool endpoints (Phase 4)."""

import pytest
from fastapi.testclient import TestClient

import main
from auth import entitlements
from tests.integration.helpers import PASSWORD, WEB, new_email

HEATMAP = "/ai-signal-heatmap/analyze"
HEATMAP_BODY = {"text": "This is a manuscript excerpt with enough words to analyse. " * 3}


def new_user(client: TestClient) -> tuple[str, str]:
    email = new_email()
    body = client.post("/auth/signup", headers=WEB, json={"email": email, "password": PASSWORD}).json()
    return email, body["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def heatmap(client: TestClient, token: str):
    return client.post(HEATMAP, headers=bearer(token), json=HEATMAP_BODY)


def feature(me_body: dict, key: str) -> dict:
    return next(f for f in me_body["features"] if f["key"] == key)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/analyse-paper"),
        ("post", "/analyse"),
        ("post", "/voice-calibrate"),
        ("post", HEATMAP),
        ("post", "/abstract-forge/generate"),
        ("post", "/rebuttal-writer/analyze"),
    ],
)
def test_tools_require_login(client, method, path):
    response = getattr(client, method)(path, json={})
    assert response.status_code == 401
    assert response.json()["code"] == "missing_token"


def test_free_plan_daily_limit_and_me_usage(client, db):
    _, token = new_user(client)
    db(lambda c: c.execute(
        "UPDATE plan_features SET daily_limit = 3 FROM plans p "
        "WHERE p.id = plan_features.plan_id AND p.key = 'free' AND feature_key = 'ai_heatmap'"
    ))
    entitlements.invalidate()
    try:
        assert [heatmap(client, token).status_code for _ in range(3)] == [200, 200, 200]
        blocked = heatmap(client, token)
        assert blocked.status_code == 429
        body = blocked.json()
        assert body["code"] == "quota_exceeded" and body["period"] == "day"
        assert body["limit"] == 3 and body["used"] == 3 and body["feature"] == "ai_heatmap"
        assert int(blocked.headers["retry-after"]) > 0

        usage = feature(client.get("/me", headers=bearer(token)).json(), "ai_heatmap")
        assert usage["used_today"] == 3 and usage["remaining_today"] == 0 and usage["daily_limit"] == 3
    finally:
        db(lambda c: c.execute(
            "UPDATE plan_features SET daily_limit = 20 FROM plans p "
            "WHERE p.id = plan_features.plan_id AND p.key = 'free' AND feature_key = 'ai_heatmap'"
        ))


def test_user_override_beats_plan(client, db):
    email, token = new_user(client)
    db(lambda c: c.execute(
        "INSERT INTO user_overrides (user_id, feature_key, daily_limit_set, daily_limit) "
        "SELECT id, 'ai_heatmap', true, 1 FROM users WHERE email = $1", email
    ))
    assert heatmap(client, token).status_code == 200
    assert heatmap(client, token).status_code == 429

    # Override back to unlimited (flag set, value NULL).
    db(lambda c: c.execute(
        "UPDATE user_overrides SET daily_limit = NULL FROM users u "
        "WHERE u.id = user_overrides.user_id AND u.email = $1", email
    ))
    assert heatmap(client, token).status_code == 200
    usage = feature(client.get("/me", headers=bearer(token)).json(), "ai_heatmap")
    assert usage["daily_limit"] is None and usage["remaining_today"] is None


def test_expired_override_is_ignored(client, db):
    email, token = new_user(client)
    db(lambda c: c.execute(
        "INSERT INTO user_overrides (user_id, feature_key, enabled, expires_at) "
        "SELECT id, 'ai_heatmap', false, now() - interval '1 minute' FROM users WHERE email = $1", email
    ))
    assert heatmap(client, token).status_code == 200


def test_feature_not_in_plan_and_global_switch(client, db):
    email, token = new_user(client)
    db(lambda c: c.execute(
        "INSERT INTO user_overrides (user_id, feature_key, enabled) "
        "SELECT id, 'ai_heatmap', false FROM users WHERE email = $1", email
    ))
    denied = heatmap(client, token)
    assert denied.status_code == 403 and denied.json()["code"] == "feature_not_in_plan"

    _, other = new_user(client)
    db(lambda c: c.execute("UPDATE features SET globally_enabled = false WHERE key = 'ai_heatmap'"))
    entitlements.invalidate()
    try:
        off = heatmap(client, other)
        assert off.status_code == 403 and off.json()["code"] == "feature_disabled"
        assert feature(client.get("/me", headers=bearer(other)).json(), "ai_heatmap")["enabled"] is False
    finally:
        db(lambda c: c.execute("UPDATE features SET globally_enabled = true WHERE key = 'ai_heatmap'"))
        entitlements.invalidate()


def test_failed_work_is_refunded(client, db, monkeypatch):
    email, token = new_user(client)

    class Broken:
        async def analyse(self, **_):
            raise RuntimeError("LLM down")

    monkeypatch.setattr(main, "get_provider_cached", lambda: Broken())
    body = {"abstract": "a", "sections": {}, "target_journal": "Nature", "citation_summary": ""}
    # In production this becomes a 500; TestClient re-raises server errors.
    with pytest.raises(RuntimeError, match="LLM down"):
        client.post("/analyse", headers=bearer(token), json=body)

    usage = feature(client.get("/me", headers=bearer(token)).json(), "analyse_paper")
    assert usage["used_today"] == 0
    statuses = db(lambda c: c.fetch(
        "SELECT e.status FROM usage_events e JOIN users u ON u.id = e.subject_id WHERE u.email = $1", email
    ))
    assert [r["status"] for r in statuses] == ["refunded"]


def test_successful_work_is_committed(client, db):
    email, token = new_user(client)
    assert heatmap(client, token).status_code == 200
    statuses = db(lambda c: c.fetch(
        "SELECT e.status, e.duration_ms FROM usage_events e JOIN users u ON u.id = e.subject_id WHERE u.email = $1",
        email,
    ))
    assert [r["status"] for r in statuses] == ["committed"] and statuses[0]["duration_ms"] is not None


def test_invalid_input_does_not_consume_quota(client):
    _, token = new_user(client)
    bad = client.post("/analyse-paper", headers=bearer(token),
                      files={"file": ("notes.txt", b"hello", "text/plain")}, data={"journal": "Nature"})
    assert bad.status_code == 400
    usage = feature(client.get("/me", headers=bearer(token)).json(), "analyse_paper")
    assert usage["used_today"] == 0


def test_public_tools_are_open_but_rate_limited(client):
    statuses = [client.post("/citations", json={"references": []}).status_code for _ in range(31)]
    assert statuses[:30] == [400] * 30  # reached the endpoint (empty list is a 400)
    assert statuses[30] == 429


def test_cors_allows_only_the_app(client):
    def preflight(origin: str):
        return client.options(HEATMAP, headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })

    ok = preflight("https://rejecta.scivalon.com")
    assert ok.headers.get("access-control-allow-origin") == "https://rejecta.scivalon.com"
    preview = preflight("https://deploy-preview-12--rejectaa.netlify.app")
    assert preview.headers.get("access-control-allow-origin") == "https://deploy-preview-12--rejectaa.netlify.app"
    evil = preflight("https://evil.netlify.app")
    assert "access-control-allow-origin" not in evil.headers
