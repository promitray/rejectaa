"""Client IP and user agent for rate limits, sessions and the audit log.

Order of trust:

1. Netlify signed proxy: when NETLIFY_PROXY_SECRET is set and ``X-Nf-Sign``
   is a valid HS256 JWS from Netlify, ``X-Nf-Client-Connection-Ip`` is the
   real browser IP (auth routes reach the API through the Netlify proxy).
2. Otherwise the right-most ``X-Forwarded-For`` entry, which is the address
   Railway's edge saw. Entries to its left are client-supplied and ignored.
3. Otherwise the socket peer (local development, tests).
"""

import ipaddress
from dataclasses import dataclass

import jwt
from fastapi import Request

from config import settings

MAX_USER_AGENT_LENGTH = 512
NETLIFY_ISSUER = "netlify"


@dataclass(frozen=True)
class RequestMeta:
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    user_agent: str | None

    @property
    def ip_key(self) -> str:
        return str(self.ip) if self.ip is not None else "unknown"


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _netlify_signature_valid(signature: str) -> bool:
    try:
        jwt.decode(
            signature,
            settings.NETLIFY_PROXY_SECRET,
            algorithms=["HS256"],
            issuer=NETLIFY_ISSUER,
            options={"require": ["iss"], "verify_aud": False},
        )
        return True
    except jwt.PyJWTError:
        return False


def client_ip(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if settings.NETLIFY_PROXY_SECRET:
        signature = request.headers.get("x-nf-sign")
        if signature and _netlify_signature_valid(signature):
            ip = _parse_ip(request.headers.get("x-nf-client-connection-ip"))
            if ip is not None:
                return ip

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = _parse_ip(forwarded.split(",")[-1])
        if ip is not None:
            return ip

    return _parse_ip(request.client.host) if request.client else None


def request_meta(request: Request) -> RequestMeta:
    agent = request.headers.get("user-agent")
    return RequestMeta(
        ip=client_ip(request),
        user_agent=agent[:MAX_USER_AGENT_LENGTH] if agent else None,
    )
