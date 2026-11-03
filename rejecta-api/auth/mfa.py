"""TOTP (RFC 6238) for admins: secrets, codes, recovery codes.

* Secrets are 160-bit base32 (the RFC 4226 recommendation), encrypted at
  rest with Fernet (AES-128-CBC + HMAC-SHA256) using MFA_ENCRYPTION_KEY.
* Codes are 6 digits, 30-second steps, accepted one step either side for
  clock drift. ``match_step`` returns the matching step so the caller can
  refuse any step at or before the last one used (replay protection).
* Recovery codes are 10 single-use codes of 50 random bits each, stored as
  HMAC-SHA256 keyed by a key derived from MFA_ENCRYPTION_KEY, so a stolen
  database alone is not enough to brute-force them.
"""

import hashlib
import hmac
import re
import secrets
import time

import pyotp
from cryptography.fernet import Fernet, InvalidToken

from config import settings

TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
TOTP_DRIFT_STEPS = 1
SECRET_LENGTH = 32  # base32 characters = 160 bits
RECOVERY_CODE_COUNT = 10
RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U
RECOVERY_GROUP_LENGTH = 5
_CODE_RE = re.compile(r"^\d{6}$")


class MfaConfigError(RuntimeError):
    """MFA_ENCRYPTION_KEY is missing or invalid."""


def _fernet() -> Fernet:
    key = settings.MFA_ENCRYPTION_KEY.strip()
    if not key:
        raise MfaConfigError("MFA_ENCRYPTION_KEY is not set. Run: python -m scripts.generate_keys --write-env")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MfaConfigError("MFA_ENCRYPTION_KEY is not a valid Fernet key") from exc


def ensure_configured() -> None:
    _fernet()


def new_secret() -> str:
    return pyotp.random_base32(length=SECRET_LENGTH)


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise MfaConfigError("Stored TOTP secret cannot be decrypted with MFA_ENCRYPTION_KEY") from exc


def provisioning_uri(secret: str, email: str) -> str:
    """otpauth:// URI for authenticator apps (rendered as a QR code)."""
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD_SECONDS).provisioning_uri(
        name=email, issuer_name=settings.MFA_ISSUER_NAME
    )


def normalize_code(code: str) -> str | None:
    cleaned = re.sub(r"\s+", "", code or "")
    return cleaned if _CODE_RE.match(cleaned) else None


def match_step(secret: str, code: str, *, now: float | None = None) -> int | None:
    """The 30-second step ``code`` belongs to (within drift), else None."""
    cleaned = normalize_code(code)
    if cleaned is None:
        return None
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD_SECONDS)
    current = int((now if now is not None else time.time()) // TOTP_PERIOD_SECONDS)
    matched = None
    for step in range(current - TOTP_DRIFT_STEPS, current + TOTP_DRIFT_STEPS + 1):
        # Check every candidate so timing does not depend on which one matched.
        if hmac.compare_digest(totp.at(step * TOTP_PERIOD_SECONDS), cleaned):
            matched = step
    return matched


def code_for_step(secret: str, step: int) -> str:
    """Code for a given step (tests and diagnostics)."""
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_PERIOD_SECONDS).at(step * TOTP_PERIOD_SECONDS)


def new_recovery_codes() -> list[str]:
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(2 * RECOVERY_GROUP_LENGTH))
        codes.append(f"{raw[:RECOVERY_GROUP_LENGTH]}-{raw[RECOVERY_GROUP_LENGTH:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return re.sub(r"[\s-]+", "", (code or "").upper())


def hash_recovery_code(code: str) -> bytes:
    key = hmac.new(settings.MFA_ENCRYPTION_KEY.encode("ascii"), b"rejecta:recovery-code:v1", hashlib.sha256).digest()
    return hmac.new(key, normalize_recovery_code(code).encode("utf-8"), hashlib.sha256).digest()
