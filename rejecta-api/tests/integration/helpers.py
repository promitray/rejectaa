"""Constants and helpers shared by integration tests."""

import uuid

WEB = {"X-Rejecta-Client": "web"}
PASSWORD = "lab-notebook-42"
COOKIE = "rejecta_rt"


def new_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"
