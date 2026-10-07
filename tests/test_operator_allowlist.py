"""Operator-only sign-in: actual signature verification using generated test keys.

No credentials or private keys are read, persisted, logged or sent over a network.
"""

import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import TokenError

from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.hosted_config import HostedSettings
from pubship.hosted_token_transport import _Response
from pubship.oauth import PlayOAuth
from pubship.operator_identity import (
    GoogleCertificateRequest,
    require_operator_grant,
    verify_google_identity,
)
from pubship.vault import Vault

EMAIL = "owner@example.test"
NONCE = "synthetic-nonce-" * 3


@pytest.fixture
def identity(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    settings = HostedSettings(
        "https://operator.example.test",
        "fake-client",
        "fake-secret",
        tmp_path / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({EMAIL}),
        new_enrollment_enabled=True,
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    def certificates(self, url, method="GET", **kwargs):
        assert url == "https://www.googleapis.com/oauth2/v1/certs"
        assert method == "GET" and not kwargs
        return _Response(200, json.dumps({"test-key": public}).encode(), {})

    monkeypatch.setattr(GoogleCertificateRequest, "__call__", certificates)
    pending = {
        "identity_nonce": NONCE,
        "client_id": "client",
        "packages": ["com.example.app"],
        "bucket": "",
        "google_scopes": [SCOPES["publisher"]],
        "params": {
            "scopes": ["play:read"],
            "code_challenge": "a" * 43,
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "redirect_uri_provided_explicitly": True,
        },
    }

    def token(**changes):
        claims = {
            "iss": "https://accounts.google.com",
            "aud": settings.google_client_id,
            "iat": int(time.time()) - 1,
            "exp": int(time.time()) + 300,
            "sub": "test-subject",
            "email": EMAIL,
            "email_verified": True,
            "nonce": NONCE,
        }
        claims.update(changes)
        return {
            "id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"}),
            "access_token": "fake-access",
            "refresh_token": "fake-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": SCOPES["publisher"],
        }

    vault = Vault(settings.database, settings.encryption_key)
    provider = PlayOAuth(settings, vault)
    yield settings, pending, token, provider
    vault.close()


def test_startup_requires_allowlist_before_reading_configuration(monkeypatch):
    monkeypatch.delenv("PUBSHIP_SERVER_ALLOWED_EMAILS", raising=False)
    reader = Mock(side_effect=AssertionError("No private configuration should be read"))
    monkeypatch.setattr("pubship.hosted_config.private_file", reader)
    with pytest.raises(PlayError, match="PUBSHIP_SERVER_ALLOWED_EMAILS"):
        HostedSettings.from_env()
    reader.assert_not_called()


@pytest.mark.parametrize(
    "emails",
    [
        frozenset(),
        frozenset({"*@example.test"}),
        frozenset({"example.test"}),
        frozenset({"owner@example.test "}),
        frozenset({""}),
        [EMAIL],
    ],
)
def test_direct_configuration_cannot_bypass_exact_allowlist(identity, emails):
    settings, _, _, _ = identity
    with pytest.raises(PlayError):
        replace(settings, allowed_emails=emails)


def test_allowed_verified_account_is_persisted_only_after_verification(identity):
    settings, pending, token, provider = identity
    destination = provider.complete_google(pending, token())
    code = parse_qs(urlsplit(destination).query)["code"][0]
    subject = provider.vault.get("codes", code)["subject"]
    grant = provider.vault.get("grants", subject)
    assert grant["verified_email"] == EMAIL
    assert grant["google_subject"] == "test-subject"
    assert "id_token" not in grant
    issued = provider.issue_tokens(subject, "client", ["play:read"])
    assert asyncio.run(provider.load_access_token(issued.access_token)) is not None
    provider.settings = replace(settings, allowed_emails=frozenset({"different@example.test"}))
    assert asyncio.run(provider.load_access_token(issued.access_token)) is None
    with pytest.raises(TokenError):
        provider.issue_tokens(subject, "client", ["play:read"])


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "outsider@example.test"},
        {"email": "OWNER@example.test"},
        {"email": "owner+alias@example.test"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"email": None},
        {"sub": ""},
        {"nonce": "other-nonce"},
        {"nonce": None},
        {"aud": "different-client"},
        {"iss": "https://attacker.example"},
        {"exp": 1},
        {"iat": 4102444800},
    ],
)
def test_rejected_identity_never_stores_google_data(identity, changes):
    _, pending, token, provider = identity
    with pytest.raises(PlayError) as error:
        provider.complete_google(pending, token(**changes))
    assert provider.vault.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    assert "example.test" not in str(error.value)
    assert "fake-access" not in str(error.value)


@pytest.mark.parametrize("fault", ["missing", "unsigned", "signature", "pending_nonce"])
def test_identity_cannot_be_supplied_by_caller_without_google_signature(identity, fault):
    settings, pending, token, provider = identity
    data = token()
    if fault == "missing":
        del data["id_token"]
    elif fault == "unsigned":
        data["id_token"] = jwt.encode(
            {"email": EMAIL, "email_verified": True}, key="", algorithm="none"
        )
    elif fault == "signature":
        parts = data["id_token"].split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        data["id_token"] = ".".join(parts)
    else:
        pending = {**pending, "identity_nonce": ""}
    with pytest.raises(PlayError):
        verify_google_identity(settings, pending, data)
    with pytest.raises(PlayError):
        provider.complete_google(pending, data)
    assert provider.vault.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


@pytest.mark.parametrize(
    "grant",
    [
        {},
        {"verified_email": EMAIL},
        {"verified_email": EMAIL, "google_subject": ""},
        {"verified_email": "outsider@example.test", "google_subject": "sub"},
    ],
)
def test_legacy_or_removed_identity_grants_are_unusable(identity, grant):
    with pytest.raises(PlayError):
        require_operator_grant(identity[0], grant)
