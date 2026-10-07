"""Synthetic connection capability and current-token boundary regressions."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import TokenError

from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.hosted_config import HostedSettings
from pubship.hosted_grants import READ_SCOPE, consent_capabilities, grant_capabilities
from pubship.oauth import PlayOAuth
from pubship.vault import Vault

METADATA = "play:publisher-metadata"
LISTINGS = "play:edit-listings"
COMMIT = "play:edit-commit"
APP = "com.example.one"
OTHER = "com.example.two"


@pytest.fixture
def provider(tmp_path, synthetic_google_identity):
    tmp_path.chmod(0o700)
    settings = HostedSettings(
        "https://mcp.example.test",
        "synthetic",
        "synthetic-secret",
        tmp_path / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({"owner@example.test"}),
        new_enrollment_enabled=True,
    )
    vault = Vault(settings.database, settings.encryption_key)
    yield PlayOAuth(settings, vault)
    vault.close()


def issue(provider, subject="one", client="client-one", *, capabilities=None, legacy=False):
    grant = {
        "verified_email": "owner@example.test",
        "google_subject": "synthetic-owner",
        "client_id": client,
        "packages": [APP, OTHER],
        "bucket": "",
        "google_scopes": [SCOPES["publisher"]],
        "access_token": "synthetic-google-access",
        "refresh_token": "test-refresh",
        "google_expires": time.time() + 3600,
        "expires": time.time() + 3600,
    }
    scopes = [READ_SCOPE]
    if not legacy:
        mapping = {READ_SCOPE: [APP, OTHER], **(capabilities or {LISTINGS: [APP]})}
        grant.update(capability_version=1, capability_packages=mapping)
        scopes = list(mapping)
    provider.vault.put("grants", subject, grant, 3600)
    tokens = provider.issue_tokens(subject, client, scopes)
    principal = asyncio.run(provider.load_access_token(tokens.access_token))
    assert principal is not None
    return grant, tokens, principal


def test_explicit_capability_packages_and_context_are_immutable(provider):
    _, _, principal = issue(provider)
    context = provider.authorize_current(principal, LISTINGS, APP)
    assert context.subject == "one"
    assert context.client_id == "client-one"
    assert context.capability_packages[READ_SCOPE] == frozenset({APP, OTHER})
    assert context.capability_packages[LISTINGS] == frozenset({APP})
    assert context.google_services == frozenset({"publisher"})
    assert context.bucket_configured is False
    with pytest.raises(TypeError):
        context.capability_packages[COMMIT] = frozenset({APP})
    with pytest.raises(FrozenInstanceError):
        context.subject = "other"
    with pytest.raises(PlayError):
        provider.authorize_current(principal, LISTINGS, OTHER)
    with pytest.raises(PlayError):
        provider.authorize_current(principal, COMMIT, APP)


def test_legacy_grant_cannot_inherit_new_scopes_or_environment(provider, monkeypatch):
    _, _, principal = issue(provider, legacy=True)
    for name in (
        "GOOGLE_PLAY_WRITE_PACKAGES",
        "GOOGLE_PLAY_EDIT_PACKAGES",
        "GOOGLE_PLAY_TRACK_PACKAGES",
    ):
        monkeypatch.setenv(name, APP)
    with patch("google.auth.default") as adc, patch("subprocess.run") as gcloud:
        context = provider.authorize_current(principal)
        settings, _ = provider.services(principal)
        assert context.scopes == frozenset({READ_SCOPE})
        assert (
            not settings.write_packages
            and not settings.edit_packages
            and not settings.track_packages
        )
        with pytest.raises(PlayError):
            provider.authorize_current(principal, LISTINGS, APP)
        with pytest.raises(TokenError):
            provider.issue_tokens("one", "client-one", [READ_SCOPE, LISTINGS])
    adc.assert_not_called()
    gcloud.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"token": "invented"},
        {"subject": "other"},
        {"client_id": "other"},
        {"resource": "https://wrong.example/mcp"},
        {"scopes": [READ_SCOPE, LISTINGS, COMMIT]},
        {"expires_at": int(time.time()) + 99999},
    ],
)
def test_forged_principal_does_not_replace_persisted_access_record(provider, changes):
    _, _, principal = issue(provider)
    forged = principal.model_copy(update=changes)
    with pytest.raises(PlayError):
        provider.authorize_current(forged)


def test_actual_access_record_expiry_and_deletion_are_rechecked(provider):
    _, _, principal = issue(provider)
    provider.vault.delete("access", principal.token)
    with pytest.raises(PlayError):
        provider.authorize_current(principal)
    expired = principal.model_copy(update={"expires_at": int(time.time()) - 1})
    provider.vault.put("access", expired.token, expired.model_dump(mode="json"), 600)
    with pytest.raises(PlayError):
        provider.authorize_current(expired)
    assert asyncio.run(provider.load_access_token(expired.token)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"capability_version": True},
        {"capability_version": 2},
        {"capability_packages": None},
        {"capability_packages": {READ_SCOPE: [APP, OTHER], COMMIT: ["com.ungranted.app"]}},
        {"capability_packages": {READ_SCOPE: [APP], LISTINGS: [APP]}},
        {"capability_packages": {READ_SCOPE: [APP, OTHER], "play:admin": [APP]}},
        {"capability_packages": {READ_SCOPE: [APP, OTHER], COMMIT: []}},
        {"google_scopes": [SCOPES["reporting"]]},
        {"google_scopes": "publisher"},
        {"expires": float("inf")},
        {"expires": True},
        {"bucket": "https://untrusted.invalid"},
    ],
)
def test_malformed_capability_grants_fail_closed_without_values_in_errors(provider, changes):
    grant, _, principal = issue(provider)
    grant.update(changes)
    # Nonfinite values are already rejected by Vault; exercise pure contract too.
    with pytest.raises(PlayError) as error:
        grant_capabilities(grant)
    assert "synthetic-google" not in str(error.value)
    if changes.get("expires") != float("inf"):
        provider.vault.put("grants", principal.subject, grant, 600)
        assert asyncio.run(provider.load_access_token(principal.token)) is None


def test_partially_versioned_grant_does_not_fall_back_to_legacy(provider):
    grant, _, _ = issue(provider)
    del grant["capability_version"]
    with pytest.raises(PlayError):
        grant_capabilities(grant)


@pytest.mark.parametrize("malformed", [["not-a-grant"], "not-a-grant", 7, {}, None])
def test_malformed_whole_grant_is_unauthorized_not_an_unhandled_error(provider, malformed):
    _, _, principal = issue(provider)
    provider.vault.put("grants", principal.subject, malformed, 600)
    with pytest.raises(PlayError):
        provider.authorize_current(principal)
    assert asyncio.run(provider.load_access_token(principal.token)) is None


def test_refresh_narrowing_persists_and_cannot_be_reexpanded(provider):
    _, tokens, _ = issue(provider, capabilities={LISTINGS: [APP], COMMIT: [APP]})
    client = SimpleNamespace(client_id="client-one")
    refresh = asyncio.run(provider.load_refresh_token(client, tokens.refresh_token))
    narrowed = asyncio.run(provider.exchange_refresh_token(client, refresh, [READ_SCOPE, LISTINGS]))
    principal = asyncio.run(provider.load_access_token(narrowed.access_token))
    assert provider.authorize_current(principal).scopes == frozenset({READ_SCOPE, LISTINGS})
    with pytest.raises(PlayError):
        provider.authorize_current(principal, COMMIT, APP)
    refresh = asyncio.run(provider.load_refresh_token(client, narrowed.refresh_token))
    forged = refresh.model_copy(update={"scopes": [READ_SCOPE, LISTINGS, COMMIT]})
    with pytest.raises(TokenError):
        asyncio.run(provider.exchange_refresh_token(client, forged, [READ_SCOPE, LISTINGS, COMMIT]))
    # An invalid scope request does not consume the legitimate refresh token.
    assert asyncio.run(provider.load_refresh_token(client, narrowed.refresh_token)) is not None


def test_disconnection_invalidates_only_its_connection_under_the_connection_lock(provider):
    _, _, one = issue(provider)
    _, _, two = issue(provider, subject="two", client="client-two")
    observations = []

    def other_thread_can_acquire(subject):
        lock = provider.grant_lock(subject)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:

        def invalidate(subject):
            assert provider.vault.get("grants", subject) is None
            assert pool.submit(other_thread_can_acquire, subject).result(timeout=2) is False
            assert pool.submit(other_thread_can_acquire, two.subject).result(timeout=2) is True
            observations.append(subject)

        provider.register_invalidator(invalidate)
        provider.disconnect(one.subject)
    assert observations == ["one"]
    with pytest.raises(PlayError):
        provider.authorize_current(one)
    assert provider.authorize_current(two).subject == "two"


def test_cleanup_failure_does_not_restore_revoked_authorization_or_leak(provider):
    _, _, principal = issue(provider)
    successful = Mock()
    provider.register_invalidator(Mock(side_effect=RuntimeError("PRIVATE-SENTINEL")))
    provider.register_invalidator(successful)
    with pytest.raises(PlayError) as error:
        provider.disconnect(principal.subject)
    assert "PRIVATE" not in str(error.value)
    successful.assert_called_once_with(principal.subject)
    assert asyncio.run(provider.load_access_token(principal.token)) is None


def test_consent_intersects_requested_scopes_and_explicit_package_choices():
    selected = consent_capabilities(
        [READ_SCOPE, LISTINGS, COMMIT],
        [LISTINGS],
        {LISTINGS: APP, COMMIT: ""},
        [APP, OTHER],
        {"publisher"},
    )
    assert selected == {READ_SCOPE: [APP, OTHER], LISTINGS: [APP]}


@pytest.mark.parametrize(
    "requested,selected,fields,services",
    [
        ([READ_SCOPE], [LISTINGS], {LISTINGS: APP}, {"publisher"}),
        ([READ_SCOPE, LISTINGS], [COMMIT], {COMMIT: APP}, {"publisher"}),
        ([READ_SCOPE, LISTINGS], [LISTINGS], {LISTINGS: OTHER}, {"publisher"}),
        ([READ_SCOPE, LISTINGS], [LISTINGS], {LISTINGS: APP}, {"reporting"}),
        ([READ_SCOPE, LISTINGS], [LISTINGS], {}, {"publisher"}),
        ([READ_SCOPE, LISTINGS], [], {LISTINGS: APP}, {"publisher"}),
        ([READ_SCOPE, LISTINGS], [LISTINGS, LISTINGS], {LISTINGS: APP}, {"publisher"}),
    ],
)
def test_consent_rejects_forged_or_implicit_grants(requested, selected, fields, services):
    with pytest.raises(PlayError):
        consent_capabilities(requested, selected, fields, [APP], services)
