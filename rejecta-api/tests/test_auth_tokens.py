"""Access-token signing and the attacks verification must reject."""

import base64
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auth import tokens
from auth.tokens import AccessTokenError, TokenConfigError, decode_access_token, issue_access_token
from config import settings
from scripts.generate_keys import new_key

USER_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()


def issue(**overrides) -> str:
    params = dict(user_id=USER_ID, role="user", token_version=3, session_id=SESSION_ID, aal=1)
    params.update(overrides)
    token, _ = issue_access_token(**params)
    return token


def valid_payload(**overrides) -> dict:
    now = int(time.time())
    payload = {
        "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE, "sub": str(USER_ID),
        "typ": "access", "role": "user", "tv": 0, "sid": str(SESSION_ID), "aal": 1,
        "iat": now, "nbf": now, "exp": now + 600, "jti": "j",
    }
    payload.update(overrides)
    return payload


def sign(payload: dict, kid: str, key=None, headers: dict | None = None) -> str:
    key = key or tokens.get_keyring().private_key
    return jwt.encode(payload, key, algorithm="EdDSA", headers={"kid": kid, "typ": "at+jwt", **(headers or {})})


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_roundtrip(signing_key):
    claims = decode_access_token(issue(role="admin", aal=2))
    assert claims.user_id == USER_ID
    assert claims.session_id == SESSION_ID
    assert (claims.role, claims.token_version, claims.aal) == ("admin", 3, 2)
    header = jwt.get_unverified_header(issue())
    assert header == {"alg": "EdDSA", "typ": "at+jwt", "kid": signing_key}


def test_expiry_is_access_ttl(signing_key):
    _, expires_in = issue_access_token(user_id=USER_ID, role="user", token_version=0, session_id=SESSION_ID, aal=1)
    assert expires_in == settings.ACCESS_TOKEN_TTL_MINUTES * 60


def test_expired_token_rejected(signing_key):
    with pytest.raises(AccessTokenError):
        decode_access_token(issue(now=time.time() - 3600))


def test_not_yet_valid_token_rejected(signing_key):
    with pytest.raises(AccessTokenError):
        decode_access_token(issue(now=time.time() + 3600))


def test_wrong_audience_and_issuer_rejected(signing_key, monkeypatch):
    token = issue()
    original_audience = settings.JWT_AUDIENCE
    monkeypatch.setattr(settings, "JWT_AUDIENCE", "someone-else")
    with pytest.raises(AccessTokenError):
        decode_access_token(token)
    monkeypatch.setattr(settings, "JWT_AUDIENCE", original_audience)
    with pytest.raises(AccessTokenError):
        decode_access_token(sign(valid_payload(iss="https://evil.example"), signing_key))


def test_alg_none_rejected(signing_key):
    token = f"{b64({'alg': 'none', 'typ': 'at+jwt', 'kid': signing_key})}.{b64(valid_payload())}."
    with pytest.raises(AccessTokenError):
        decode_access_token(token)


def test_hs256_key_confusion_rejected(signing_key):
    public_raw = tokens.get_keyring().private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    forged = jwt.encode(valid_payload(role="admin"), public_raw, algorithm="HS256",
                        headers={"kid": signing_key, "typ": "at+jwt"})
    with pytest.raises(AccessTokenError):
        decode_access_token(forged)


def test_unknown_kid_and_foreign_key_rejected(signing_key):
    stranger = Ed25519PrivateKey.generate()
    with pytest.raises(AccessTokenError):
        decode_access_token(sign(valid_payload(), "unknown-kid", key=stranger))
    with pytest.raises(AccessTokenError):
        decode_access_token(sign(valid_payload(role="admin"), signing_key, key=stranger))


def test_tampered_payload_rejected(signing_key):
    header, _, signature = issue().split(".")
    with pytest.raises(AccessTokenError):
        decode_access_token(f"{header}.{b64(valid_payload(role='admin'))}.{signature}")


@pytest.mark.parametrize(
    "overrides",
    [
        {"typ": "refresh"},
        {"role": "superadmin"},
        {"aal": 3},
        {"tv": True},
        {"tv": -1},
        {"sid": "not-a-uuid"},
        {"sub": "not-a-uuid"},
    ],
)
def test_bad_claims_rejected(signing_key, overrides):
    with pytest.raises(AccessTokenError):
        decode_access_token(sign(valid_payload(**overrides), signing_key))


def test_missing_required_claim_rejected(signing_key):
    payload = valid_payload()
    del payload["jti"]
    with pytest.raises(AccessTokenError):
        decode_access_token(sign(payload, signing_key))


def test_wrong_typ_header_rejected(signing_key):
    with pytest.raises(AccessTokenError):
        decode_access_token(sign(valid_payload(), signing_key, headers={"typ": "JWT"}))


def test_garbage_and_oversized_tokens_rejected(signing_key):
    for bad in ("", "abc", "a.b.c", "x" * 5000):
        with pytest.raises(AccessTokenError):
            decode_access_token(bad)


def test_previous_key_accepted_during_rotation(signing_key, monkeypatch):
    old_kid, old_private, old_public = new_key()
    monkeypatch.setattr(settings, "JWT_PREVIOUS_PUBLIC_KEYS", f"{old_kid}:{old_public}")
    tokens.reset_keyring()
    old_key = tokens.load_private_key(old_private)
    assert decode_access_token(sign(valid_payload(), old_kid, key=old_key)).user_id == USER_ID

    monkeypatch.setattr(settings, "JWT_PREVIOUS_PUBLIC_KEYS", "")
    tokens.reset_keyring()
    with pytest.raises(AccessTokenError):
        decode_access_token(sign(valid_payload(), old_kid, key=old_key))


def test_jwks_lists_public_keys_only(signing_key):
    keys = tokens.get_keyring().jwks()["keys"]
    assert len(keys) == 1
    assert keys[0]["kid"] == signing_key
    assert set(keys[0]) == {"kty", "crv", "x", "kid", "use", "alg"}
    assert keys[0]["kty"] == "OKP" and keys[0]["crv"] == "Ed25519"


def test_missing_or_bad_key_config(monkeypatch):
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", "")
    tokens.reset_keyring()
    with pytest.raises(TokenConfigError):
        tokens.get_keyring()
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", "dG9vLXNob3J0")
    monkeypatch.setattr(settings, "JWT_KEY_ID", "k1")
    tokens.reset_keyring()
    with pytest.raises(TokenConfigError):
        tokens.get_keyring()
    tokens.reset_keyring()
