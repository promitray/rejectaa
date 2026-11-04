"""Create the first admin, or promote an existing user. Never possible via the API.

    python -m scripts.create_admin --email you@example.com --name "Your Name"
    python -m scripts.create_admin --email existing@example.com --promote
    python -m scripts.create_admin --email admin@example.com --reset-mfa

--reset-mfa is the break-glass path when an admin has lost both the
authenticator and the recovery codes: it removes MFA and signs the admin
out; their next login starts enrollment again.

The password is read with a hidden prompt (asked twice). For automation it
can come from the REJECTA_ADMIN_PASSWORD environment variable instead.
The same password policy as sign-up applies.
"""

import argparse
import asyncio
import getpass
import os
import sys

from auth.passwords import PasswordPolicyError, hash_password, validate_new_password
from db.repos import audit, users
from db.repos import mfa as mfa_repo
from scripts._common import connect_api


def read_password(email: str) -> str:
    from_env = os.environ.get("REJECTA_ADMIN_PASSWORD")
    password = from_env if from_env else getpass.getpass("Admin password: ")
    try:
        validate_new_password(password, email=email)
    except PasswordPolicyError as exc:
        raise SystemExit(f"Password rejected: {exc}") from exc
    if not from_env and getpass.getpass("Repeat password: ") != password:
        raise SystemExit("Passwords do not match.")
    return password


async def main(email: str, name: str, promote: bool, reset_mfa: bool = False) -> None:
    conn = await connect_api()
    try:
        existing = await users.get_by_email(conn, email)
        if reset_mfa:
            if existing is None:
                raise SystemExit("No account with this email.")
            async with conn.transaction():
                await mfa_repo.reset(conn, existing.id)
                await audit.record(
                    conn, action="admin.mfa_reset_by_script", actor_type="system",
                    target_type="user", target_id=existing.id,
                )
            print(f"OK: two-factor removed for {existing.email}; they must enroll again at next login.")
            return

        if existing is not None:
            if not promote:
                raise SystemExit("This email already has an account. Use --promote to make it an admin.")
            async with conn.transaction():
                await users.promote_to_admin(conn, existing.id)
                await audit.record(
                    conn, action="admin.promoted_by_script", actor_type="system",
                    target_type="user", target_id=existing.id,
                    before={"role": existing.role, "status": existing.status},
                    after={"role": "admin", "status": "active"},
                )
            promoted = await users.get_by_id(conn, existing.id)
            plan = await users.get_plan_summary(conn, promoted.plan_id) if promoted else None
            print(f"OK: {existing.email} is now an admin on the {plan.name if plan else '?'} plan "
                  "(existing sessions were signed out).")
            return

        if promote:
            raise SystemExit("No account with this email to promote.")
        password_hash = await hash_password(read_password(email))
        async with conn.transaction():
            user = await users.create_user(
                conn, email=email, password_hash=password_hash, full_name=name.strip(), role="admin"
            )
            await audit.record(
                conn, action="admin.created_by_script", actor_type="system",
                target_type="user", target_id=user.id, after={"email": user.email, "role": "admin"},
            )
        plan = await users.get_plan_summary(conn, user.plan_id)
        print(f"OK: admin {user.email} created on the {plan.name if plan else '?'} plan.")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--reset-mfa", action="store_true")
    args = parser.parse_args()
    if "@" not in args.email:
        sys.exit("Enter a valid email.")
    asyncio.run(main(args.email.strip().lower(), args.name, args.promote, args.reset_mfa))
