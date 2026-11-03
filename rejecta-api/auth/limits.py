"""Rate limits for auth routes (per process; see auth/rate_limit.py)."""

from auth.rate_limit import SlidingWindowLimiter

MINUTE = 60.0
HOUR = 3600.0

signup_per_ip = SlidingWindowLimiter(5, HOUR)
login_per_ip = SlidingWindowLimiter(20, 5 * MINUTE)
# Failed attempts per email, whether or not the account exists, so the
# response for unknown emails matches the one for locked accounts.
login_failures_per_email = SlidingWindowLimiter(5, 15 * MINUTE)
refresh_per_ip = SlidingWindowLimiter(60, MINUTE)
password_change_per_user = SlidingWindowLimiter(5, 15 * MINUTE)
password_reset_per_ip = SlidingWindowLimiter(10, 15 * MINUTE)
# Admin TOTP: attempts per MFA token (one password login), failed codes per
# admin across tokens, and MFA tokens already used (single use, by jti).
mfa_attempts_per_token = SlidingWindowLimiter(5, 10 * MINUTE)
mfa_failures_per_user = SlidingWindowLimiter(10, 15 * MINUTE)
mfa_tokens_consumed = SlidingWindowLimiter(1, 15 * MINUTE)

# Free unauthenticated tools (/parse, /citations, /journal-match).
public_per_ip = SlidingWindowLimiter(30, MINUTE)

# Partner token endpoint.
oauth_per_ip = SlidingWindowLimiter(30, MINUTE)
oauth_failures_per_client = SlidingWindowLimiter(10, 15 * MINUTE)

ALL = (
    oauth_per_ip,
    oauth_failures_per_client,
    public_per_ip,
    signup_per_ip,
    login_per_ip,
    login_failures_per_email,
    refresh_per_ip,
    password_change_per_user,
    password_reset_per_ip,
    mfa_attempts_per_token,
    mfa_failures_per_user,
    mfa_tokens_consumed,
)


def reset_all() -> None:
    """Clear every window (tests)."""
    for limiter in ALL:
        limiter.clear()
