"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure rejecta-api root is always on sys.path across all platforms & CI runners
API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest
from auth import tokens
from config import settings
from scripts.generate_keys import new_key


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure a fresh Ed25519 signing key; returns its kid."""
    kid, private_b64, _ = new_key()
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", private_b64)
    monkeypatch.setattr(settings, "JWT_KEY_ID", kid)
    monkeypatch.setattr(settings, "JWT_PREVIOUS_PUBLIC_KEYS", "")
    tokens.reset_keyring()
    yield kid
    tokens.reset_keyring()
