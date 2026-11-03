"""Sliding-window limiter and trusted client-IP extraction."""

import ipaddress

import jwt
import pytest
from starlette.requests import Request

from auth.rate_limit import SlidingWindowLimiter
from auth.request_meta import client_ip, request_meta
from config import settings


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limit_and_window_slide():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(3, 60, clock=clock)
    assert [limiter.hit("k") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.hit("k") == pytest.approx(60.0)
    clock.now += 30
    assert limiter.hit("k") == pytest.approx(30.0)
    clock.now += 30.001
    assert limiter.hit("k") == 0.0


def test_keys_are_independent_and_reset_works():
    limiter = SlidingWindowLimiter(1, 60, clock=FakeClock())
    assert limiter.hit("a") == 0.0
    assert limiter.hit("b") == 0.0
    assert limiter.hit("a") > 0
    limiter.reset("a")
    assert limiter.hit("a") == 0.0


def test_retry_after_does_not_record():
    limiter = SlidingWindowLimiter(1, 60, clock=FakeClock())
    for _ in range(5):
        assert limiter.retry_after("k") == 0.0
    limiter.record("k")
    assert limiter.retry_after("k") > 0


def test_memory_is_bounded():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(5, 60, max_keys=100, clock=clock)
    for i in range(1000):
        clock.now += 0.001
        limiter.hit(f"key-{i}")
    assert len(limiter._events) <= 100


def make_request(headers: dict[str, str], peer: str = "10.0.0.9") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 5555),
    }
    return Request(scope)


def test_peer_address_without_proxy_headers():
    assert client_ip(make_request({})) == ipaddress.ip_address("10.0.0.9")


def test_rightmost_forwarded_for_wins_over_spoofed_entries():
    request = make_request({"X-Forwarded-For": "6.6.6.6, 203.0.113.7"})
    assert client_ip(request) == ipaddress.ip_address("203.0.113.7")


def test_garbage_forwarded_for_falls_back_to_peer():
    assert client_ip(make_request({"X-Forwarded-For": "not-an-ip"})) == ipaddress.ip_address("10.0.0.9")


def test_netlify_ip_trusted_only_with_valid_signature(monkeypatch):
    monkeypatch.setattr(settings, "NETLIFY_PROXY_SECRET", "s3cret")
    good = jwt.encode({"iss": "netlify", "site_url": "https://rejecta.scivalon.com"}, "s3cret", algorithm="HS256")
    bad = jwt.encode({"iss": "netlify"}, "wrong", algorithm="HS256")
    headers = {"X-Nf-Client-Connection-Ip": "198.51.100.4", "X-Forwarded-For": "203.0.113.7"}

    assert client_ip(make_request({**headers, "X-Nf-Sign": good})) == ipaddress.ip_address("198.51.100.4")
    assert client_ip(make_request({**headers, "X-Nf-Sign": bad})) == ipaddress.ip_address("203.0.113.7")
    assert client_ip(make_request(headers)) == ipaddress.ip_address("203.0.113.7")


def test_netlify_header_ignored_when_secret_unset(monkeypatch):
    monkeypatch.setattr(settings, "NETLIFY_PROXY_SECRET", "")
    request = make_request({"X-Nf-Client-Connection-Ip": "198.51.100.4", "X-Nf-Sign": "anything"})
    assert client_ip(request) == ipaddress.ip_address("10.0.0.9")


def test_user_agent_is_truncated():
    meta = request_meta(make_request({"User-Agent": "u" * 2000}))
    assert meta.user_agent is not None and len(meta.user_agent) == 512
