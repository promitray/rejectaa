"""Helpers for Postgres connection URLs.

Supabase passwords often contain characters such as ``@``, ``/`` or ``#``
that break a URL unless they are percent-encoded. These helpers accept a URL
with either a raw or an already percent-encoded password and always return a
correctly encoded one.

A literal ``%`` in a raw password must be written as ``%25``.
"""

from urllib.parse import quote, unquote

POSTGRES_SCHEMES = ("postgresql", "postgres")
SQLALCHEMY_ASYNC_SCHEME = "postgresql+asyncpg"
SSL_MODES = ("require", "disable")


class DatabaseUrlError(ValueError):
    """Raised when a database URL cannot be understood."""


def normalize_postgres_url(raw: str) -> str:
    """Return ``postgresql://user:password@host:port/db`` with encoded credentials.

    Raises:
        DatabaseUrlError: if the value is empty, not a Postgres URL, has no
            credentials or host, or carries a query string (TLS is controlled
            by ``DB_SSL_MODE`` instead, so both drivers behave the same).
    """
    value = raw.strip()
    if not value:
        raise DatabaseUrlError("Database URL is empty")

    scheme, separator, rest = value.partition("://")
    if not separator or scheme.split("+", 1)[0].lower() not in POSTGRES_SCHEMES:
        raise DatabaseUrlError("Database URL must start with postgresql://")

    credentials, at, location = rest.rpartition("@")
    if not at or not credentials:
        raise DatabaseUrlError("Database URL must include user and password")

    user, colon, password = credentials.partition(":")
    if not user or not colon:
        raise DatabaseUrlError("Database URL must include user and password")

    host_port, slash, database = location.partition("/")
    if not host_port:
        raise DatabaseUrlError("Database URL must include a host")
    if "?" in database or "#" in database or "?" in host_port:
        raise DatabaseUrlError(
            "Remove query parameters from the database URL; set DB_SSL_MODE instead"
        )
    database = database if slash and database else "postgres"

    encoded_user = quote(unquote(user), safe="")
    encoded_password = quote(unquote(password), safe="")
    return f"postgresql://{encoded_user}:{encoded_password}@{host_port}/{database}"


def sqlalchemy_async_url(raw: str) -> str:
    """Return the URL in the form SQLAlchemy's asyncpg dialect expects."""
    normalized = normalize_postgres_url(raw)
    return SQLALCHEMY_ASYNC_SCHEME + normalized[len("postgresql") :]


def asyncpg_ssl(mode: str) -> str | bool:
    """Map ``DB_SSL_MODE`` to the value asyncpg accepts for ``ssl``."""
    normalized = mode.strip().lower()
    if normalized not in SSL_MODES:
        raise DatabaseUrlError(f"DB_SSL_MODE must be one of {list(SSL_MODES)}")
    return "require" if normalized == "require" else False
