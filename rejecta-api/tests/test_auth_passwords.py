"""Argon2id hashing and the password policy."""

import pytest

from auth.passwords import PasswordPolicyError, hash_password, validate_new_password, verify_password

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_hash_and_verify():
    hashed = await hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert await verify_password(hashed, "correct horse battery")
    assert not await verify_password(hashed, "correct horse batterY")


async def test_same_password_gets_different_salts():
    assert await hash_password("same password 1") != await hash_password("same password 1")


async def test_verify_never_raises_on_bad_hash():
    assert not await verify_password("not-a-hash", "whatever")
    assert not await verify_password("", "")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short1!", "at least 10"),
        ("x" * 129, "at most 128"),
        ("password123", "too common"),
        ("QwertyUiop", "too common"),
        ("aaaaaaaaaaaa", "too simple"),
        ("someone@example.com", "email"),
        ("someone12345", None),
    ],
)
async def test_policy(password, message):
    if message is None:
        validate_new_password(password, email="someone@example.com")
        return
    with pytest.raises(PasswordPolicyError, match=message):
        validate_new_password(password, email="someone@example.com")


async def test_local_part_of_email_rejected():
    with pytest.raises(PasswordPolicyError):
        validate_new_password("dr.researcher", email="Dr.Researcher@uni.edu")


async def test_long_passphrase_with_spaces_allowed():
    validate_new_password("my lab notebook is blue", email="a@b.co")
