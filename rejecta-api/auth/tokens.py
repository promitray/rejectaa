"""Access tokens: JWT signed with Ed25519 (alg ``EdDSA``).

Header: ``{"alg": "EdDSA", "typ": "at+jwt", "kid": "<key id>"}``
Claims: iss, aud, sub (user id), typ="access", role, tv (token_version),
sid (session id), aal (1 = password, 2 = password + TOTP), iat, nbf, exp, jti.

Verification is strict: only ``EdDSA`` (so ``none``/HS256 confusion is
impossible), the ``kid`` must be a known key, and iss, aud, exp, nbf, typ and
every required claim are checked. The role in the token is informational;
authorisation always re-reads the user from the database.
"""

import base64
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from config import settings

ALGORITHM = "EdDSA"
HEADER_TYP = "at+jwt"
ACCESS_TOKEN_TYPE = "access"
LEEWAY_SECONDS = 30
MAX_TOKEN_LENGTH = 4096
VALID_ROLES = ("admin", "user")
VALID_AAL = (1, 2)
REQUIRED_CLAIMS = ["iss", "aud", "sub", "exp", "iat", "nbf", "jti"]


class TokenConfigError(RuntimeError):
    """Signing keys are missing or invalid (server misconfiguration)."""


class AccessTokenError(Exception):
    """The presented token is not a valid access token. Never shown to clients."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    role: str
    token_version: int
    session_id: uuid.UUID
    aal: int
    expires_at: int
    jti: str


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padded = value.strip() + "=" * (-len(value.strip()) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def load_private_key(raw: str) -> Ed25519PrivateKey:
    value = raw.strip()
    try:
        if value.startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(value.replace("\\n", "\n").encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise TokenConfigError("JWT_PRIVATE_KEY must be an Ed25519 key")
            return key
        seed = b64url_decode(value)
    except TokenConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TokenConfigError("JWT_PRIVATE_KEY is not a valid Ed25519 key") from exc
    if len(seed) != 32:
        raise TokenConfigError("JWT_PRIVATE_KEY must be 32 bytes (base64url)")
    return Ed25519PrivateKey.from_private_bytes(seed)


def load_public_key(raw: str) -> Ed25519PublicKey:
    try:
        data = b64url_decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise TokenConfigError("Invalid public key in JWT_PREVIOUS_PUBLIC_KEYS") from exc
    if len(data) != 32:
        raise TokenConfigError("Ed25519 public keys must be 32 bytes")
    return Ed25519PublicKey.from_public_bytes(data)


def public_key_b64(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64url_encode(raw)


class KeyRing:
    """The current signing key plus public keys accepted for verification."""

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        kid: str,
        previous: dict[str, Ed25519PublicKey] | None = None,
    ) -> None:
        if not kid or len(kid) > 64:
            raise TokenConfigError("JWT_KEY_ID must be 1-64 characters")
        self.private_key = private_key
        self.kid = kid
        self.verify_keys: dict[str, Ed25519PublicKey] = dict(previous or {})
        self.verify_keys[kid] = private_key.public_key()

    @classmethod
    def from_settings(cls) -> "KeyRing":
        if not settings.JWT_PRIVATE_KEY.strip() or not settings.JWT_KEY_ID.strip():
            raise TokenConfigError(
                "JWT_PRIVATE_KEY / JWT_KEY_ID are not set. Run: python -m scripts.generate_keys --write-env"
            )
        previous: dict[str, Ed25519PublicKey] = {}
        for item in settings.JWT_PREVIOUS_PUBLIC_KEYS.split(","):
            if not item.strip():
                continue
            kid, sep, encoded = item.strip().partition(":")
            if not sep or not kid:
                raise TokenConfigError("JWT_PREVIOUS_PUBLIC_KEYS entries must be kid:base64url")
            previous[kid] = load_public_key(encoded)
        return cls(load_private_key(settings.JWT_PRIVATE_KEY), settings.JWT_KEY_ID.strip(), previous)

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        return {
            "keys": [
                {"kty": "OKP", "crv": "Ed25519", "x": public_key_b64(key), "kid": kid, "use": "sig", "alg": ALGORITHM}
                for kid, key in self.verify_keys.items()
            ]
        }


_keyring: KeyRing | None = None


def get_keyring() -> KeyRing:
    global _keyring
    if _keyring is None:
        _keyring = KeyRing.from_settings()
    return _keyring


def reset_keyring() -> None:
    """Forget the cached keys (tests, key rotation)."""
    global _keyring
    _keyring = None


def issue_access_token(
    *,
    user_id: uuid.UUID,
    role: str,
    token_version: int,
    session_id: uuid.UUID,
    aal: int,
    now: float | None = None,
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``."""
    keyring = get_keyring()
    issued_at = int(now if now is not None else time.time())
    expires_in = settings.ACCESS_TOKEN_TTL_MINUTES * 60
    payload = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": str(user_id),
        "typ": ACCESS_TOKEN_TYPE,
        "role": role,
        "tv": token_version,
        "sid": str(session_id),
        "aal": aal,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + expires_in,
        "jti": secrets.token_urlsafe(16),
    }
    token = jwt.encode(
        payload,
        keyring.private_key,
        algorithm=ALGORITHM,
        headers={"kid": keyring.kid, "typ": HEADER_TYP},
    )
    return token, expires_in


MFA_HEADER_TYP = "mfa+jwt"
MFA_TOKEN_TYPE = "mfa"
MFA_PURPOSES = ("enroll", "verify")


@dataclass(frozen=True)
class MfaClaims:
    user_id: uuid.UUID
    token_version: int
    purpose: str
    jti: str
    expires_at: int


def _mfa_audience() -> str:
    # A different audience from access tokens: an MFA token is never accepted
    # where an access token is expected, and vice versa.
    return f"{settings.JWT_AUDIENCE}:mfa"


def issue_mfa_token(*, user_id: uuid.UUID, token_version: int, purpose: str) -> tuple[str, int]:
    """Short-lived proof that the password step passed. Returns (token, expires_in)."""
    if purpose not in MFA_PURPOSES:
        raise ValueError("purpose must be 'enroll' or 'verify'")
    keyring = get_keyring()
    issued_at = int(time.time())
    expires_in = settings.MFA_TOKEN_TTL_MINUTES * 60
    payload = {
        "iss": settings.JWT_ISSUER,
        "aud": _mfa_audience(),
        "sub": str(user_id),
        "typ": MFA_TOKEN_TYPE,
        "purpose": purpose,
        "tv": token_version,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + expires_in,
        "jti": secrets.token_urlsafe(16),
    }
    token = jwt.encode(payload, keyring.private_key, algorithm=ALGORITHM,
                       headers={"kid": keyring.kid, "typ": MFA_HEADER_TYP})
    return token, expires_in


def decode_mfa_token(token: str) -> MfaClaims:
    """Verify an MFA token or raise AccessTokenError."""
    claims = _verify(token, header_typ=MFA_HEADER_TYP, audience=_mfa_audience())
    try:
        if claims.get("typ") != MFA_TOKEN_TYPE or claims.get("purpose") not in MFA_PURPOSES:
            raise ValueError("typ/purpose")
        tv = claims["tv"]
        if type(tv) is not int or tv < 0:
            raise ValueError("tv")
        return MfaClaims(
            user_id=uuid.UUID(claims["sub"]),
            token_version=tv,
            purpose=claims["purpose"],
            jti=str(claims["jti"]),
            expires_at=int(claims["exp"]),
        )
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise AccessTokenError("invalid claims") from exc


PARTNER_HEADER_TYP = "partner+jwt"
PARTNER_TOKEN_TYPE = "client"


@dataclass(frozen=True)
class PartnerClaims:
    client_uuid: uuid.UUID
    client_id: str
    token_version: int
    scopes: tuple[str, ...]
    expires_at: int


def _partner_audience() -> str:
    return f"{settings.JWT_AUDIENCE}:partner"


def issue_partner_token(
    *, client_uuid: uuid.UUID, client_id: str, token_version: int, scopes: list[str]
) -> tuple[str, int]:
    """Access token for a partner server (OAuth2 client credentials)."""
    keyring = get_keyring()
    issued_at = int(time.time())
    expires_in = settings.ACCESS_TOKEN_TTL_MINUTES * 60
    payload = {
        "iss": settings.JWT_ISSUER,
        "aud": _partner_audience(),
        "sub": str(client_uuid),
        "cid": client_id,
        "typ": PARTNER_TOKEN_TYPE,
        "scope": " ".join(scopes),
        "tv": token_version,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + expires_in,
        "jti": secrets.token_urlsafe(16),
    }
    token = jwt.encode(payload, keyring.private_key, algorithm=ALGORITHM,
                       headers={"kid": keyring.kid, "typ": PARTNER_HEADER_TYP})
    return token, expires_in


def decode_partner_token(token: str) -> PartnerClaims:
    claims = _verify(token, header_typ=PARTNER_HEADER_TYP, audience=_partner_audience())
    try:
        if claims.get("typ") != PARTNER_TOKEN_TYPE:
            raise ValueError("typ")
        tv = claims["tv"]
        if type(tv) is not int or tv < 0:
            raise ValueError("tv")
        scope = claims["scope"]
        if not isinstance(scope, str):
            raise ValueError("scope")
        return PartnerClaims(
            client_uuid=uuid.UUID(claims["sub"]),
            client_id=str(claims["cid"]),
            token_version=tv,
            scopes=tuple(s for s in scope.split(" ") if s),
            expires_at=int(claims["exp"]),
        )
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise AccessTokenError("invalid claims") from exc


def token_header_typ(token: str) -> str | None:
    """Unverified ``typ`` header, only to route a token to the right verifier."""
    try:
        typ = jwt.get_unverified_header(token).get("typ")
    except jwt.PyJWTError:
        return None
    return typ if isinstance(typ, str) else None


def _verify(token: str, *, header_typ: str, audience: str) -> dict:
    """Signature, header and registered-claim checks shared by all token types."""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise AccessTokenError("missing or oversized token")
    keyring = get_keyring()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AccessTokenError("malformed header") from exc
    if header.get("alg") != ALGORITHM or header.get("typ") != header_typ:
        raise AccessTokenError("unexpected alg/typ")
    kid = header.get("kid")
    key = keyring.verify_keys.get(kid) if isinstance(kid, str) else None
    if key is None:
        raise AccessTokenError("unknown kid")
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=settings.JWT_ISSUER,
            leeway=LEEWAY_SECONDS,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise AccessTokenError(type(exc).__name__) from exc


def decode_access_token(token: str) -> AccessClaims:
    """Verify ``token`` and return its claims, or raise AccessTokenError."""
    claims = _verify(token, header_typ=HEADER_TYP, audience=settings.JWT_AUDIENCE)
    try:
        if claims.get("typ") != ACCESS_TOKEN_TYPE:
            raise ValueError("typ")
        role, tv, aal = claims["role"], claims["tv"], claims["aal"]
        if role not in VALID_ROLES:
            raise ValueError("role")
        if type(tv) is not int or tv < 0:
            raise ValueError("tv")
        if type(aal) is not int or aal not in VALID_AAL:
            raise ValueError("aal")
        return AccessClaims(
            user_id=uuid.UUID(claims["sub"]),
            role=role,
            token_version=tv,
            session_id=uuid.UUID(claims["sid"]),
            aal=aal,
            expires_at=int(claims["exp"]),
            jti=str(claims["jti"]),
        )
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise AccessTokenError("invalid claims") from exc
