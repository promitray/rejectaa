"""Generate the auth secrets: JWT signing key and the MFA encryption key.

    python -m scripts.generate_keys               # print values for Railway
    python -m scripts.generate_keys --write-env   # write missing ones into rejecta-api/.env
    python -m scripts.generate_keys --write-env --rotate

* JWT signing key (Ed25519): created if missing. ``--rotate`` replaces it and
  keeps the old public key in JWT_PREVIOUS_PUBLIC_KEYS, so tokens signed
  with it stay valid until they expire (15 minutes). Remove that entry later.
* MFA_ENCRYPTION_KEY (Fernet): created if missing, never rotated by this
  script (rotating it would break every admin's authenticator).

Secrets are never printed when --write-env is used.
"""

import argparse
import secrets
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auth.tokens import b64url_encode, load_private_key, public_key_b64
from config import settings
from scripts._common import ENV_FILE, update_env_file


def new_key() -> tuple[str, str, str]:
    """Return (kid, private_seed_b64url, public_b64url)."""
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    kid = f"k{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}"
    return kid, b64url_encode(seed), public_key_b64(key.public_key())


def main(write_env: bool, rotate: bool) -> None:
    values: dict[str, str] = {}

    has_jwt = bool(settings.JWT_PRIVATE_KEY.strip())
    if not has_jwt or rotate:
        kid, private_b64, _ = new_key()
        previous = settings.JWT_PREVIOUS_PUBLIC_KEYS.strip()
        if has_jwt:
            old_public = public_key_b64(load_private_key(settings.JWT_PRIVATE_KEY).public_key())
            entry = f"{settings.JWT_KEY_ID.strip()}:{old_public}"
            previous = ",".join(item for item in (previous, entry) if item)
        values.update(JWT_PRIVATE_KEY=private_b64, JWT_KEY_ID=kid, JWT_PREVIOUS_PUBLIC_KEYS=previous)
    else:
        print(f"JWT signing key {settings.JWT_KEY_ID} kept (use --rotate to replace it).")

    if not settings.MFA_ENCRYPTION_KEY.strip():
        values["MFA_ENCRYPTION_KEY"] = Fernet.generate_key().decode("ascii")
    else:
        print("MFA_ENCRYPTION_KEY kept.")

    if not values:
        return
    if write_env:
        for key, value in values.items():
            update_env_file(key, value)
        print(f"OK: wrote {', '.join(values)} to {ENV_FILE}")
        if values.get("JWT_PREVIOUS_PUBLIC_KEYS"):
            print("JWT_PREVIOUS_PUBLIC_KEYS keeps the old key; remove it after 15 minutes.")
    else:
        print("Set these environment variables (keep them secret):")
        for key, value in values.items():
            print(f"{key}={value}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write-env", action="store_true")
    parser.add_argument("--rotate", action="store_true", help="replace the JWT signing key")
    args = parser.parse_args()
    main(args.write_env, args.rotate)
