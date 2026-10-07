"""Explicit synthetic identity boundary for existing OAuth protocol fixtures.

Cryptographic ID-token and allowlist acceptance have their own tests without this
fixture. No fixture reads credentials or calls Google's identity endpoints.
"""

import pytest


@pytest.fixture
def synthetic_google_identity(monkeypatch):
    def verify(settings, pending, token):
        return "owner@example.test", "synthetic-owner"

    monkeypatch.setattr("pubship.oauth.verify_google_identity", verify)
