"""HTTP-level checks that /parse accepts Word files and rejects other types with clear messages.

/parse is the public endpoint (IP rate limit only), so it needs no login. /analyse-paper shares
the same name check and parser; its logic after parsing is covered elsewhere.
"""
import io

import pytest
from fastapi.testclient import TestClient

from auth import limits
from main import app
from test_parser_docx import REFERENCES, build_docx, make_pdf

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def client():
    limits.reset_all()  # /parse is limited to 30 requests per minute per IP
    yield TestClient(app)
    limits.reset_all()


def upload(client, name, data, content_type="application/octet-stream"):
    return client.post("/parse", files={"file": (name, io.BytesIO(data), content_type)})


def test_parse_accepts_a_word_file(client):
    response = upload(client, "manuscript.docx", build_docx(), DOCX_TYPE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["references"] == REFERENCES
    assert "methods" in body["sections"]


def test_parse_still_accepts_a_pdf(client):
    response = upload(client, "paper.pdf", make_pdf("Abstract\nA short abstract that is long enough to be picked up here."), "application/pdf")
    assert response.status_code == 200, response.text


def test_parse_rejects_legacy_doc_with_a_helpful_message(client):
    response = upload(client, "old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    assert response.status_code == 400
    assert ".docx" in response.json()["detail"]


def test_parse_rejects_other_file_types(client):
    response = upload(client, "notes.txt", b"hello")
    assert response.status_code == 400
    assert "PDF or a Word" in response.json()["detail"]


def test_parse_rejects_a_fake_docx(client):
    response = upload(client, "fake.docx", b"this is not a zip file")
    assert response.status_code == 400


def test_parse_rejects_a_corrupt_docx(client):
    response = upload(client, "broken.docx", build_docx()[:300])
    assert response.status_code == 400
    assert "Could not open" in response.json()["detail"]
