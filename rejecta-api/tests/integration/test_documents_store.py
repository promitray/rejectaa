"""Working copies in Postgres: versions, undo, proposals, owners, expiry (needs TEST_DATABASE_URL)."""

import uuid

import pytest

from services.documents.store import Owner, PostgresDocumentStore, VersionConflict

OWNER = Owner("user", uuid.uuid4())
OTHER = Owner("client", uuid.uuid4())
SECTIONS = [{"id": "s1", "name": "introduction", "start": 1, "end": 2}]


@pytest.fixture
def store(client):
    from db.pool import get_pool

    def call(method, *args):
        async def run():
            return await getattr(PostgresDocumentStore(get_pool()), method)(*args)

        return client.portal.call(run)

    return call


def test_versions_undo_and_owners(store):
    created = store("create", OWNER, "paper.docx", "docx", b"v1-bytes", SECTIONS)
    assert created.version == 1 and created.docx == b"v1-bytes" and created.sections == SECTIONS
    assert store("get", OTHER, created.id) is None

    second = store("add_version", OWNER, created.id, 1, b"v2-bytes", SECTIONS, "intro calibrated")
    assert second.version == 2 and second.docx == b"v2-bytes"
    with pytest.raises(VersionConflict):
        store("add_version", OWNER, created.id, 1, b"stale", SECTIONS, "late")

    back = store("drop_latest_version", OWNER, created.id)
    assert back.version == 1 and back.docx == b"v1-bytes"
    assert store("drop_latest_version", OWNER, created.id) is None  # nothing older than the original

    again = store("add_version", OWNER, created.id, 1, b"v2-again", SECTIONS, "redo")
    assert again.version == 2 and again.docx == b"v2-again"


def test_proposals_belong_to_the_owner(store):
    created = store("create", OWNER, "paper.pdf", "pdf", b"bytes", SECTIONS)
    proposal_id = store("save_proposal", OWNER, created.id, "s1", 1, {"masked_output": "text"})
    assert store("get_proposal", OTHER, created.id, proposal_id) is None
    proposal = store("get_proposal", OWNER, created.id, proposal_id)
    assert proposal.status == "open" and proposal.payload == {"masked_output": "text"}
    store("set_proposal_status", OTHER, created.id, proposal_id, "accepted")  # ignored: not the owner
    assert store("get_proposal", OWNER, created.id, proposal_id).status == "open"
    store("set_proposal_status", OWNER, created.id, proposal_id, "accepted")
    assert store("get_proposal", OWNER, created.id, proposal_id).status == "accepted"
    with pytest.raises(KeyError):
        store("save_proposal", OTHER, created.id, "s1", 1, {})


def test_delete_and_expiry(store, db):
    gone = store("create", OWNER, "a.docx", "docx", b"a", SECTIONS)
    assert store("delete", OTHER, gone.id) is False
    assert store("delete", OWNER, gone.id) is True
    assert store("get", OWNER, gone.id) is None

    old = store("create", OWNER, "b.docx", "docx", b"b", SECTIONS)
    db(lambda conn: conn.execute("UPDATE documents SET expires_at = now() - interval '1 minute' WHERE id = $1", old.id))
    assert store("get", OWNER, old.id) is None  # expired copies are never served
    assert store("purge_expired") >= 1
    assert db(lambda conn: conn.fetchval("SELECT count(*) FROM document_versions WHERE document_id = $1", old.id)) == 0
