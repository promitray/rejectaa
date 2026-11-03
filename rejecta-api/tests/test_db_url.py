"""Unit tests for database URL normalisation (db/url.py) and the API-role URL builder."""

import pytest

from db.url import DatabaseUrlError, asyncpg_ssl, normalize_postgres_url, sqlalchemy_async_url
from scripts.db_role import api_database_url, scram_sha256_verifier

DIRECT_HOST = "db.abcdefghijklmnop.supabase.co:5432"
POOLER_HOST = "aws-0-eu-west-1.pooler.supabase.com:5432"


def test_raw_at_sign_in_password_is_encoded():
    url = normalize_postgres_url(f"postgresql://postgres:pa@ss@{DIRECT_HOST}/postgres")
    assert url == f"postgresql://postgres:pa%40ss@{DIRECT_HOST}/postgres"


def test_already_encoded_password_is_unchanged():
    url = f"postgresql://postgres:pa%40ss@{DIRECT_HOST}/postgres"
    assert normalize_postgres_url(url) == url


def test_slash_hash_and_colon_in_password():
    url = normalize_postgres_url(f"postgresql://postgres:a/b#c:d@{DIRECT_HOST}/postgres")
    assert url == f"postgresql://postgres:a%2Fb%23c%3Ad@{DIRECT_HOST}/postgres"


def test_postgres_scheme_and_missing_database_default():
    assert normalize_postgres_url(f"postgres://u:p@{DIRECT_HOST}") == f"postgresql://u:p@{DIRECT_HOST}/postgres"


def test_sqlalchemy_url_uses_asyncpg_driver():
    assert sqlalchemy_async_url(f"postgresql://u:p@{DIRECT_HOST}/postgres").startswith("postgresql+asyncpg://u:p@")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "mysql://u:p@host/db",
        f"postgresql://{DIRECT_HOST}/postgres",
        f"postgresql://useronly@{DIRECT_HOST}/postgres",
        f"postgresql://u:p@{DIRECT_HOST}/postgres?sslmode=require",
        "postgresql://u:p@/postgres",
    ],
)
def test_invalid_urls_are_rejected(bad: str):
    with pytest.raises(DatabaseUrlError):
        normalize_postgres_url(bad)


def test_ssl_modes():
    assert asyncpg_ssl("require") == "require"
    assert asyncpg_ssl("DISABLE") is False
    with pytest.raises(DatabaseUrlError):
        asyncpg_ssl("prefer")


def test_api_url_direct_host_uses_plain_role():
    url = api_database_url(f"postgresql://postgres:x@y@{DIRECT_HOST}/postgres", "secret")
    assert url == f"postgresql://rejecta_api:secret@{DIRECT_HOST}/postgres"


def test_api_url_pooler_host_keeps_project_ref():
    url = api_database_url(f"postgresql://postgres.abcdefghijklmnop:x@{POOLER_HOST}/postgres", "secret")
    assert url == f"postgresql://rejecta_api.abcdefghijklmnop:secret@{POOLER_HOST}/postgres"


def test_scram_verifier_format_and_randomness():
    first = scram_sha256_verifier("password")
    second = scram_sha256_verifier("password")
    assert first.startswith("SCRAM-SHA-256$4096:")
    assert first != second  # random salt
    assert "'" not in first
