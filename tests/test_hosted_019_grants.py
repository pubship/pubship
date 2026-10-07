"""App/customer consent must not be inherited from catalog or financial authority."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.auth.provider import TokenError
from test_hosted_grants import APP, OTHER, issue
from test_hosted_grants import provider as provider

from pubship.errors import PlayError
from pubship.hosted_catalog import (
    HOSTED_CAPABILITIES,
    HOSTED_PURCHASE_CAPABILITIES,
    HOSTED_SENSITIVE_READ_CAPABILITIES,
    HOSTED_SUPPORT_CAPABILITIES,
    HOSTED_TESTER_WRITE_METHODS,
)
from pubship.hosted_grants import READ_SCOPE, consent_capabilities

NEW = (
    "play:review-replies",
    "play:app-declarations",
    "play:app-operations",
    "play:tester-audience",
    "play:customer-data",
    "play:purchase-actions",
    "play:external-transactions",
    "play:subscriber-price-migration",
)


def test_customer_methods_have_separate_authority_and_complete_disjoint_inventory():
    groups = [
        set(HOSTED_SUPPORT_CAPABILITIES),
        set(HOSTED_PURCHASE_CAPABILITIES),
        set(HOSTED_SENSITIVE_READ_CAPABILITIES),
        set(HOSTED_TESTER_WRITE_METHODS),
    ]
    assert sum(map(len, groups)) == len(set.union(*groups)) == 31
    assert len(HOSTED_CAPABILITIES) == 170
    assert {cap: list(HOSTED_CAPABILITIES.values()).count(cap) for cap in NEW} == {
        "play:review-replies": 1,
        "play:app-declarations": 1,
        "play:app-operations": 6,
        "play:tester-audience": 3,
        "play:customer-data": 6,
        "play:purchase-actions": 10,
        "play:external-transactions": 2,
        "play:subscriber-price-migration": 2,
    }
    assert HOSTED_CAPABILITIES["androidpublisher.orders.refund"] == "play:purchase-actions"
    assert HOSTED_CAPABILITIES["androidpublisher.orders.get"] == "play:customer-data"
    assert HOSTED_CAPABILITIES["androidpublisher.edits.testers.get"] == "play:tester-audience"
    assert (
        HOSTED_CAPABILITIES["androidpublisher.monetization.subscriptions.basePlans.migratePrices"]
        == "play:subscriber-price-migration"
    )


@pytest.mark.parametrize("capability", NEW)
def test_new_consent_needs_client_request_publisher_service_and_exact_app(capability):
    expected = {READ_SCOPE: sorted([APP, OTHER]), capability: [APP]}
    args = ([READ_SCOPE, capability], [capability], {capability: APP}, [APP, OTHER])
    assert consent_capabilities(*args, ["publisher"]) == expected
    assert consent_capabilities([READ_SCOPE, capability], [], {}, [APP, OTHER], ["publisher"]) == {
        READ_SCOPE: sorted([APP, OTHER])
    }
    with pytest.raises(PlayError):
        consent_capabilities([READ_SCOPE], [capability], {capability: APP}, [APP], ["publisher"])
    with pytest.raises(PlayError):
        consent_capabilities(*args, ["reporting"])
    with pytest.raises(PlayError):
        consent_capabilities(
            [READ_SCOPE, capability], [capability], {capability: OTHER}, [APP], ["publisher"]
        )


@pytest.mark.parametrize("capability", NEW)
def test_legacy_and_catalog_connections_cannot_gain_customer_authority(provider, capability):
    for index, options in enumerate(
        [{"legacy": True}, {"capabilities": {"play:subscription-catalog": [APP]}}]
    ):
        _, _, principal = issue(provider, subject=f"connection-{index}", **options)
        with pytest.raises(PlayError):
            provider.authorize_current(principal, capability, APP)
        with pytest.raises(TokenError):
            provider.issue_tokens(principal.subject, principal.client_id, [READ_SCOPE, capability])


@pytest.mark.parametrize("capability", NEW)
def test_grant_narrowing_survives_refresh_and_cannot_be_reexpanded(provider, capability):
    _, tokens, principal = issue(provider, capabilities={capability: [APP]})
    provider.authorize_current(principal, capability, APP)
    with pytest.raises(PlayError):
        provider.authorize_current(principal, capability, OTHER)
    for other in set(NEW) - {capability}:
        with pytest.raises(PlayError):
            provider.authorize_current(principal, other, APP)
    client = SimpleNamespace(client_id=principal.client_id)
    refresh = asyncio.run(provider.load_refresh_token(client, tokens.refresh_token))
    narrowed = asyncio.run(provider.exchange_refresh_token(client, refresh, [READ_SCOPE]))
    updated = asyncio.run(provider.load_access_token(narrowed.access_token))
    with pytest.raises(PlayError):
        provider.authorize_current(updated, capability, APP)
    refresh = asyncio.run(provider.load_refresh_token(client, narrowed.refresh_token))
    with pytest.raises(TokenError):
        asyncio.run(provider.exchange_refresh_token(client, refresh, [READ_SCOPE, capability]))
