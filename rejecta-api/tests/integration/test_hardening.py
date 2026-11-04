"""Security headers, request ids, and recovery of abandoned reservations (Phase 9)."""

import uuid

from auth.usage import refund_stale_reservations
from tests.integration.helpers import PASSWORD, WEB, new_email


def test_security_headers_and_request_id(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert len(response.headers["x-request-id"]) >= 8

    echoed = client.get("/health", headers={"X-Request-Id": "abcd1234-trace"})
    assert echoed.headers["x-request-id"] == "abcd1234-trace"
    bogus = client.get("/health", headers={"X-Request-Id": "<script>"})
    assert bogus.headers["x-request-id"] != "<script>"


def test_sensitive_responses_are_not_cached(client):
    response = client.post("/auth/login", headers=WEB, json={"email": new_email(), "password": PASSWORD})
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/me").headers["cache-control"] == "no-store"


def test_signup_records_first_sign_in(client, db):
    email = new_email()
    client.post("/auth/signup", headers=WEB, json={"email": email, "password": PASSWORD})
    assert db(lambda c: c.fetchval("SELECT last_login_at IS NOT NULL FROM users WHERE email = $1", email))


def test_abandoned_reservations_are_refunded(client, db):
    subject = uuid.uuid4()
    row = db(lambda c: c.fetchrow("SELECT * FROM consume_quota('user', $1, 'ai_heatmap', 5, NULL)", subject))
    db(lambda c: c.execute(
        "UPDATE usage_events SET created_at = now() - interval '2 hours' WHERE id = $1", row["event_id"]))
    refunded = client.portal.call(refund_stale_reservations)
    assert refunded >= 1
    status = db(lambda c: c.fetchval("SELECT status FROM usage_events WHERE id = $1", row["event_id"]))
    count = db(lambda c: c.fetchval(
        "SELECT count FROM usage_counters WHERE subject_id = $1 AND period = 'day'", subject))
    assert (status, count) == ("refunded", 0)
