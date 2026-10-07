"""Independent store dimensions never widen ordinary application authority."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import TokenError
from test_hosted_grants import APP, OTHER, issue
from test_hosted_grants import provider as provider

from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.hosted_grants import (
    APPSTORE_EVENTS,
    APPSTORE_PAIR_SCOPES,
    READ_SCOPE,
    consent_authority,
    grant_authority,
    grant_capabilities,
)

STORE = "com.example.store"
SECOND_STORE = "com.example.secondstore"
CATALOG = "play:appstore-catalog"
MEDIA = "play:appstore-media"


def store_connection(provider, *, pairs=True, events=False, packages=None):
    grant, _, _ = issue(provider)
    grant.update(
        capability_version=2,
        packages=packages or [],
        capability_packages={READ_SCOPE: packages or []},
        appstore_targets={CATALOG: {STORE: [APP], SECOND_STORE: [OTHER]}} if pairs else {},
        appstore_event_stores=[STORE] if events else [],
    )
    provider.vault.put("grants", "one", grant, 3600)
    authority = grant_authority(grant)
    tokens = provider.issue_tokens("one", "client-one", list(authority.scopes))
    principal = asyncio.run(provider.load_access_token(tokens.access_token))
    assert principal is not None
    return grant, tokens, principal


@pytest.mark.parametrize("pair_only", [True, False])
def test_store_only_authority_preserves_empty_base_and_survives_refresh(provider, pair_only):
    grant, tokens, principal = store_connection(provider, pairs=pair_only, events=not pair_only)
    for phase in range(2):
        context = provider.authorize_current(principal)
        assert context.authorization_schema_version == 2
        assert context.capability_packages == {READ_SCOPE: frozenset()}
        settings, _ = provider.services(principal)
        assert not settings.packages
        with pytest.raises(PlayError):
            provider.authorize_current(principal, READ_SCOPE, APP)
        if pair_only:
            provider.authorize_appstore_current(principal, CATALOG, STORE, APP)
            provider.authorize_appstore_current(principal, CATALOG, SECOND_STORE, OTHER)
            for store, app in [(STORE, OTHER), (SECOND_STORE, APP), (APP, STORE)]:
                with pytest.raises(PlayError):
                    provider.authorize_appstore_current(principal, CATALOG, store, app)
            with pytest.raises(PlayError):
                provider.authorize_appstore_current(principal, APPSTORE_EVENTS, STORE)
        else:
            provider.authorize_appstore_current(principal, APPSTORE_EVENTS, STORE)
            with pytest.raises(PlayError):
                provider.authorize_appstore_current(principal, APPSTORE_EVENTS, STORE, APP)
            with pytest.raises(PlayError):
                provider.authorize_appstore_current(principal, CATALOG, STORE, APP)
        if phase == 0:
            client = SimpleNamespace(client_id=principal.client_id)
            refresh = asyncio.run(provider.load_refresh_token(client, tokens.refresh_token))
            tokens = asyncio.run(
                provider.exchange_refresh_token(client, refresh, list(context.scopes))
            )
            principal = asyncio.run(provider.load_access_token(tokens.access_token))
    assert provider.vault.get("grants", "one") == grant


def test_store_context_is_deeply_immutable(provider):
    _, _, principal = store_connection(provider)
    context = provider.authorize_current(principal)
    with pytest.raises(TypeError):
        context.appstore_targets[MEDIA] = {}
    with pytest.raises(TypeError):
        context.appstore_targets[CATALOG][STORE] = frozenset({OTHER})
    assert context.appstore_targets[CATALOG][STORE] == frozenset({APP})


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize(
    "capability",
    ["play:edit-artifacts", "play:artifact-downloads", "play:internal-sharing", CATALOG],
)
def test_legacy_versions_reject_new_capabilities_and_extension_smuggling(
    provider, version, capability
):
    grant, _, _ = issue(provider, legacy=version == 0)
    forged = copy.deepcopy(grant)
    if version:
        forged["capability_packages"][capability] = [APP]
    else:
        forged["appstore_targets"] = {CATALOG: {STORE: [APP]}}
    with pytest.raises(PlayError):
        grant_authority(forged)
    assert grant_capabilities(grant)[READ_SCOPE] == frozenset({APP, OTHER})


@pytest.mark.parametrize(
    "changes",
    [
        {"appstore_targets": None},
        {"appstore_event_stores": None},
        {"appstore_targets": {CATALOG: {}}},
        {"appstore_targets": {CATALOG: {STORE: []}}},
        {"appstore_targets": {CATALOG: {STORE: [APP, APP]}}},
        {"appstore_targets": {APPSTORE_EVENTS: {STORE: [APP]}}},
        {"appstore_targets": {"play:edit-commit": {STORE: [APP]}}},
        {"appstore_targets": {CATALOG: {"https://private.invalid": [APP]}}},
        {"appstore_event_stores": [STORE, STORE]},
        {"google_scopes": [SCOPES["reporting"]]},
        {"capability_packages": {READ_SCOPE: [], MEDIA: [APP]}},
        {"capability_packages": {READ_SCOPE: [], "play:edit-artifacts": [APP]}},
        {"appstore_targets": {}, "appstore_event_stores": []},
        {"capability_version": 3},
        {"capability_version": True},
    ],
)
def test_invalid_dimensions_fail_closed_with_safe_error(provider, changes):
    grant, _, principal = store_connection(provider)
    grant.update(changes)
    with pytest.raises(PlayError, match="invalid, expired or revoked"):
        grant_authority(grant)
    provider.vault.put("grants", "one", grant, 600)
    assert asyncio.run(provider.load_access_token(principal.token)) is None


def test_missing_v2_extension_is_not_silently_inferred(provider):
    grant, _, _ = store_connection(provider)
    for field in ("appstore_targets", "appstore_event_stores"):
        incomplete = {key: value for key, value in grant.items() if key != field}
        with pytest.raises(PlayError):
            grant_authority(incomplete)


def test_current_store_revocation_and_scope_narrowing_are_effective(provider):
    grant, tokens, principal = store_connection(provider, events=True)
    client = SimpleNamespace(client_id=principal.client_id)
    refresh = asyncio.run(provider.load_refresh_token(client, tokens.refresh_token))
    tokens = asyncio.run(provider.exchange_refresh_token(client, refresh, [READ_SCOPE, CATALOG]))
    current = asyncio.run(provider.load_access_token(tokens.access_token))
    assert not provider.authorize_current(current).appstore_event_stores
    with pytest.raises(PlayError):
        provider.authorize_appstore_current(current, APPSTORE_EVENTS, STORE)
    with pytest.raises(TokenError):
        refresh = asyncio.run(provider.load_refresh_token(client, tokens.refresh_token))
        asyncio.run(
            provider.exchange_refresh_token(client, refresh, [READ_SCOPE, CATALOG, APPSTORE_EVENTS])
        )
    grant["appstore_targets"][CATALOG][STORE] = [OTHER]
    provider.vault.put("grants", "one", grant, 600)
    with pytest.raises(PlayError):
        provider.authorize_appstore_current(current, CATALOG, STORE, APP)
    provider.authorize_appstore_current(current, CATALOG, STORE, OTHER)


@pytest.mark.parametrize("capability", sorted(APPSTORE_PAIR_SCOPES))
def test_explicit_pair_consent_has_no_cross_product_or_base_read(capability):
    choice = consent_authority(
        [READ_SCOPE, capability],
        [capability],
        {},
        [],
        ["publisher"],
        {
            capability: f"{STORE}/{APP}, {SECOND_STORE}/{OTHER}",
        },
    )
    assert choice["capability_packages"] == {READ_SCOPE: []}
    assert choice["appstore_targets"] == {capability: {STORE: [APP], SECOND_STORE: [OTHER]}}
    assert choice["appstore_event_stores"] == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"{STORE}/{APP}, {STORE}/{APP}",
        f"{STORE}/{APP}, {STORE} / {APP}",
        STORE,
        f"{STORE}/{APP}/extra",
    ],
)
def test_pair_consent_rejects_empty_duplicate_or_ambiguous_values(text):
    with pytest.raises(PlayError):
        consent_authority([READ_SCOPE, CATALOG], [CATALOG], {}, [], ["publisher"], {CATALOG: text})


def test_consent_requires_requested_selected_capability_and_publisher():
    for requested, selected, services in [
        ([READ_SCOPE], [CATALOG], ["publisher"]),
        ([READ_SCOPE, CATALOG], [], ["publisher"]),
        ([READ_SCOPE, CATALOG], [CATALOG], ["reporting"]),
    ]:
        with pytest.raises(PlayError):
            consent_authority(requested, selected, {}, [], services, {CATALOG: f"{STORE}/{APP}"})
    with pytest.raises(PlayError):
        consent_authority([READ_SCOPE], [], {}, [], ["publisher"], {})


def test_event_only_consent_is_explicitly_store_wide():
    choice = consent_authority(
        [READ_SCOPE, APPSTORE_EVENTS],
        [APPSTORE_EVENTS],
        {},
        [],
        ["publisher"],
        {APPSTORE_EVENTS: STORE},
    )
    assert choice["appstore_targets"] == {}
    assert choice["appstore_event_stores"] == [STORE]
    assert choice["capability_packages"] == {READ_SCOPE: []}


def test_pair_quota_is_shared_across_capabilities(provider):
    grant, _, _ = store_connection(provider)
    apps = [f"com.example.app{i}" for i in range(100)]
    grant["appstore_targets"] = {CATALOG: {STORE: apps}, MEDIA: {STORE: apps}}
    assert grant_authority(grant)
    grant["appstore_targets"][MEDIA][SECOND_STORE] = [APP]
    with pytest.raises(PlayError):
        grant_authority(grant)
