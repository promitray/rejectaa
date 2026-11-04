"""Password hashing (Argon2id) and the password policy.

Hashing is CPU and memory heavy (~64 MiB per hash with argon2-cffi's
RFC 9106 low-memory defaults), so it runs in worker threads behind a small
capacity limiter: at most HASH_CONCURRENCY hashes at once, which bounds
memory on a small Railway instance and keeps the event loop free.

Policy follows NIST SP 800-63B: length matters, composition rules do not.
"""

import secrets

import anyio
from anyio import to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
MIN_DISTINCT_CHARACTERS = 4
HASH_CONCURRENCY = 4

# Common passwords that satisfy the length rule. Small on purpose; it blocks
# the most obvious choices without shipping a large wordlist.
COMMON_PASSWORDS = frozenset(
    {
        "1234567890", "0123456789", "9876543210", "1111111111", "0000000000",
        "1234512345", "12345678910", "123456789a", "1234567890a", "a123456789",
        "password12", "password01", "password123", "password1234", "password!1",
        "password@123", "passw0rd123", "p@ssw0rd123", "qwertyuiop", "qwertyuiop1",
        "qwerty12345", "qwerty@123", "1q2w3e4r5t", "q1w2e3r4t5", "1qaz2wsx3edc",
        "qazwsxedc123", "asdfghjkl1", "asdfghjkl123", "zxcvbnm123", "abcdefghij",
        "abcd123456", "abc1234567", "iloveyou12", "iloveyou123", "welcome123",
        "welcome@123", "letmein123", "changeme123", "admin12345", "admin@123",
        "administrator", "sunshine123", "football123", "baseball123", "monkey12345",
        "princess123", "superman123", "starwars123", "dragon12345", "master12345",
        "shadow12345", "computer123", "whatever123", "internet123", "trustno1234",
        "india12345", "india@123", "samsung1234", "rejecta123", "rejecta1234",
    }
)

_hasher = PasswordHasher()  # argon2id
_limiter: anyio.CapacityLimiter | None = None
# Verified when the email is unknown, so a miss costs as much as a hit and
# response time does not reveal whether an account exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(24))


class PasswordPolicyError(ValueError):
    """Raised with a user-facing message when a new password is rejected."""


def _get_limiter() -> anyio.CapacityLimiter:
    # Created lazily: anyio needs a running event loop to build a limiter.
    global _limiter
    if _limiter is None:
        _limiter = anyio.CapacityLimiter(HASH_CONCURRENCY)
    return _limiter


def validate_new_password(password: str, *, email: str | None = None) -> None:
    """Raise PasswordPolicyError if ``password`` is not acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        raise PasswordPolicyError("This password is too common. Choose a less predictable one.")
    if len(set(password)) < MIN_DISTINCT_CHARACTERS:
        raise PasswordPolicyError("This password is too simple. Use more varied characters.")
    if email:
        normalized = email.strip().lower()
        if lowered in {normalized, normalized.split("@", 1)[0]}:
            raise PasswordPolicyError("Password must not be your email address.")


async def hash_password(password: str) -> str:
    return await to_thread.run_sync(_hasher.hash, password, limiter=_get_limiter())


async def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verification; never raises for bad input."""

    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await to_thread.run_sync(_verify, limiter=_get_limiter())


async def verify_dummy(password: str) -> None:
    """Spend the same time as a real verification, for unknown accounts."""
    await verify_password(_DUMMY_HASH, password)
