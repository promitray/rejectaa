"""Server-side pagination of the admin lists (users, partners, audit log)."""

import math
import uuid

import pytest

from tests.integration.admin_helpers import admin_session, bearer
from tests.integration.helpers import PASSWORD, WEB, new_email


@pytest.fixture
def headers(client, db) -> dict[str, str]:
    _, token = admin_session(client, db)
    return bearer(token)


def test_users_are_paged_with_stable_order_and_total(client, headers):
    tag = uuid.uuid4().hex[:8]
    for i in range(5):
        client.post("/auth/signup", headers=WEB, json={"email": f"page-{tag}-{i}@example.com", "password": PASSWORD})

    seen: list[str] = []
    for page in (1, 2, 3):
        body = client.get("/admin/users", headers=headers, params={"search": f"page-{tag}", "page": page, "page_size": 2}).json()
        assert body["total"] == 5 and body["page"] == page and body["page_size"] == 2
        assert len(body["items"]) == (2 if page < 3 else 1)
        seen += [u["email"] for u in body["items"]]
    assert len(seen) == len(set(seen)) == 5  # no overlap, nothing skipped

    beyond = client.get("/admin/users", headers=headers, params={"search": f"page-{tag}", "page": 99, "page_size": 2}).json()
    assert beyond["items"] == [] and beyond["total"] == 5


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_invalid_paging_is_rejected(client, headers, params):
    assert client.get("/admin/users", headers=headers, params=params).status_code == 422
    assert client.get("/admin/partners", headers=headers, params=params).status_code == 422


def test_partners_are_paged_searched_and_filtered(client, headers):
    plan = next(p["id"] for p in client.get("/admin/plans", headers=headers).json() if p["key"] == "partner")
    tag = uuid.uuid4().hex[:8]
    created = []
    for i in range(3):
        response = client.post("/admin/partners", headers=headers, json={
            "name": f"Lab {tag} {i}", "plan_id": plan, "scopes": ["ai_heatmap"]})
        assert response.status_code == 201
        created.append(response.json()["partner"]["id"])

    first = client.get("/admin/partners", headers=headers, params={"search": tag, "page_size": 2}).json()
    second = client.get("/admin/partners", headers=headers, params={"search": tag, "page_size": 2, "page": 2}).json()
    assert first["total"] == second["total"] == 3
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert {p["id"] for p in first["items"] + second["items"]} == set(created)

    client.patch(f"/admin/partners/{created[0]}", headers=headers, json={"status": "disabled"})
    disabled = client.get("/admin/partners", headers=headers, params={"search": tag, "status": "disabled"}).json()
    assert [p["id"] for p in disabled["items"]] == [created[0]]

    # LIKE wildcards in the search box are matched literally.
    assert client.get("/admin/partners", headers=headers, params={"search": "%"}).json()["total"] == 0


def test_audit_log_pages_backwards_without_gaps(client, headers):
    client.post("/auth/signup", headers=WEB, json={"email": new_email(), "password": PASSWORD})
    pages, cursor = [], None
    for _ in range(50):
        params = {"limit": 5} | ({"before_id": cursor} if cursor else {})
        body = client.get("/admin/audit-log", headers=headers, params=params).json()
        pages.append([e["id"] for e in body["items"]])
        cursor = body["next_before_id"]
        if cursor is None or len(pages) >= 3:
            break
    ids = [i for page in pages for i in page]
    assert ids == sorted(ids, reverse=True) and len(ids) == len(set(ids))
    assert math.isfinite(len(ids)) and len(pages[0]) == 5
