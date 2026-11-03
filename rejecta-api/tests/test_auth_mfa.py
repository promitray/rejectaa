"""TOTP helpers, secret encryption, recovery codes and MFA tokens."""

import time
import uuid

import pytest
from cryptography.fernet import Fernet

from auth import mfa
from auth.tokens import (
    AccessTokenError,
    decode_access_token,
    decode_mfa_token,
    issue_access_token,
    issue_mfa_token,
)
from config import settings


@pytest.fixture
def mfa_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEY", key)
    return key


def test_secret_is_160_bits_base32():
    secret = mfa.new_secret()
    assert len(secret) == 32 and secret.isalnum() and secret.upper() == secret


def test_secret_encryption_roundtrip_and_wrong_key(mfa_key, monkeypatch):
    secret = mfa.new_secret()
    stored = mfa.encrypt_secret(secret)
    assert secret not in stored
    assert mfa.decrypt_secret(stored) == secret
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(mfa.MfaConfigError):
        mfa.decrypt_secret(stored)


def test_missing_or_invalid_key(monkeypatch):
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEY", "")
    with pytest.raises(mfa.MfaConfigError):
        mfa.ensure_configured()
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(mfa.MfaConfigError):
        mfa.ensure_configured()


def test_match_step_accepts_one_step_drift_only():
    secret = mfa.new_secret()
    now = 1_800_000_015.0
    step = int(now // 30)
    assert mfa.match_step(secret, mfa.code_for_step(secret, step), now=now) == step
    assert mfa.match_step(secret, mfa.code_for_step(secret, step - 1), now=now) == step - 1
    assert mfa.match_step(secret, mfa.code_for_step(secret, step + 1), now=now) == step + 1
    for far in (step - 3, step + 3):
        code = mfa.code_for_step(secret, far)
        if code not in {mfa.code_for_step(secret, s) for s in (step - 1, step, step + 1)}:
            assert mfa.match_step(secret, code, now=now) is None


def test_match_step_input_handling():
    secret = mfa.new_secret()
    code = mfa.code_for_step(secret, int(time.time() // 30))
    assert mfa.match_step(secret, f"{code[:3]} {code[3:]}") is not None
    for bad in ("", "12345", "1234567", "abcdef", "12 34 5"):
        assert mfa.match_step(secret, bad) is None


def test_provisioning_uri():
    uri = mfa.provisioning_uri("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", "admin@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP" in uri and "issuer=Rejecta" in uri


def test_recovery_codes(mfa_key, monkeypatch):
    codes = mfa.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c) == 11 and c[5] == "-" for c in codes)
    code = codes[0]
    assert mfa.hash_recovery_code(code) == mfa.hash_recovery_code(f"  {code.lower().replace('-', ' ')} ")
    assert mfa.hash_recovery_code(code) != mfa.hash_recovery_code(codes[1])
    before = mfa.hash_recovery_code(code)
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    assert mfa.hash_recovery_code(code) != before  # keyed: DB alone cannot verify codes


def test_mfa_token_roundtrip_and_isolation_from_access_tokens(signing_key):
    user_id = uuid.uuid4()
    token, expires_in = issue_mfa_token(user_id=user_id, token_version=4, purpose="enroll")
    assert expires_in == settings.MFA_TOKEN_TTL_MINUTES * 60
    claims = decode_mfa_token(token)
    assert (claims.user_id, claims.token_version, claims.purpose) == (user_id, 4, "enroll")

    with pytest.raises(AccessTokenError):
        decode_access_token(token)  # an MFA token is never an access token
    access, _ = issue_access_token(user_id=user_id, role="admin", token_version=4, session_id=uuid.uuid4(), aal=2)
    with pytest.raises(AccessTokenError):
        decode_mfa_token(access)  # and vice versa
    with pytest.raises(ValueError):
        issue_mfa_token(user_id=user_id, token_version=0, purpose="admin")
