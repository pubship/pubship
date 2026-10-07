"""Independent lifecycle and disclosure tests; all provider traffic is synthetic."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest
from mcp import Client

from pubship import purchase, purchase_http
from pubship.config import Settings
from pubship.coverage import (
    EXTERNAL_TRANSACTION_METHODS,
    PRICE_MIGRATION_METHODS,
    PURCHASE_ACTION_METHODS,
)
from pubship.errors import PlayError
from pubship.publishing import _APPLY_LOCK
from pubship.purchase import PurchaseLifecycle
from pubship.server import create_server

APP = "com.example.app"
PREFIX = "androidpublisher."
ACK = PREFIX + "purchases.products.acknowledge"
TOKEN = "synthetic-private-purchase-token"
AUTH = "synthetic-private-original-credential"
PAYLOAD = "synthetic-private-developer-payload"
ACCOUNT = "synthetic-private-account-identifier"
PARAMETERS = {"packageName": APP, "productId": "coins", "token": TOKEN}
BODY = {"developerPayload": PAYLOAD}
PRODUCT = {
    "productId": "coins",
    "purchaseToken": TOKEN,
    "purchaseState": 0,
    "acknowledgementState": 0,
    "consumptionState": 0,
    "developerPayload": PAYLOAD,
    "obfuscatedExternalAccountId": ACCOUNT,
}


def configured(**overrides):
    values = {
        "packages": frozenset({APP}),
        "sensitive_read_packages": frozenset({APP}),
        "purchase_packages": frozenset({APP}),
        "purchase_methods": PURCHASE_ACTION_METHODS,
        "external_transaction_packages": frozenset({APP}),
        "external_transaction_methods": EXTERNAL_TRANSACTION_METHODS,
        "price_migration_packages": frozenset({APP}),
        "price_migration_methods": PRICE_MIGRATION_METHODS,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def no_real_provider_traffic(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open",
        Mock(side_effect=AssertionError("No live provider traffic in lifecycle QA")),
    )


@pytest.fixture
def service(monkeypatch):
    auth = Mock()
    auth.token.return_value = AUTH
    svc = PurchaseLifecycle(configured(), auth, local=True)
    calls = []
    state = {"resource": deepcopy(PRODUCT), "mutation": {}, "post_hook": None}

    def send(url, credential, *, method="GET", body=None, **kwargs):
        calls.append((method, url, credential, deepcopy(body)))
        if method == "POST":
            if state["post_hook"] is not None:
                state["post_hook"]()
            value = state["mutation"]
        else:
            value = state["resource"]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    monkeypatch.setattr(purchase_http, "_send", send)
    return svc, auth, calls, state


def prepare(svc):
    return svc.prepare(ACK, deepcopy(PARAMETERS), deepcopy(BODY), True)


def apply(svc, prepared):
    return svc.apply(prepared["operation_id"], prepared["operation_id"])


def assert_private(value):
    serialized = json.dumps(value)
    for sentinel in (TOKEN, AUTH, PAYLOAD, ACCOUNT):
        assert sentinel not in serialized
    assert "https://androidpublisher.googleapis.com/" not in serialized


def test_original_credential_and_frozen_intent_survive_caller_changes(service):
    svc, auth, calls, _ = service
    parameters, body = deepcopy(PARAMETERS), deepcopy(BODY)
    prepared = svc.prepare(ACK, parameters, body, True)
    parameters["token"] = "replacement-purchase-token"
    body["developerPayload"] = "replacement-payload"
    auth.token.return_value = "replacement-google-account"
    result = apply(svc, prepared)
    assert auth.token.call_count == 1
    assert [entry[0] for entry in calls] == ["GET", "GET", "POST", "GET"]
    assert {entry[2] for entry in calls} == {AUTH}
    assert calls[2][3] == BODY
    assert TOKEN in calls[2][1]
    assert result["mutation_succeeded"] is True
    assert result["readback_succeeded"] is True
    assert_private(prepared)
    assert_private(result)


def test_private_preparation_representation_omits_sensitive_values(service):
    svc, _, _, _ = service
    prepared = prepare(svc)
    private = repr(svc._preparations[prepared["operation_id"]])
    for sentinel in (TOKEN, AUTH, PAYLOAD, ACCOUNT):
        assert sentinel not in private


@pytest.mark.parametrize(
    "changes",
    [
        {"purchase_packages": frozenset()},
        {"purchase_methods": frozenset()},
        {"sensitive_read_packages": frozenset()},
    ],
)
def test_grants_are_rechecked_before_apply_and_operation_stays_consumed(service, changes):
    svc, _, calls, _ = service
    prepared = prepare(svc)
    original = svc.settings
    svc.settings = replace(original, **changes)
    with pytest.raises(PlayError):
        apply(svc, prepared)
    assert len(calls) == 1
    svc.settings = original
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    assert len(calls) == 1


def test_scope_revocation_during_preflight_prevents_mutation(service, monkeypatch):
    svc, _, calls, _ = service
    prepared = prepare(svc)
    original_observe = purchase_http.observe

    def observe(*args, **kwargs):
        value = original_observe(*args, **kwargs)
        svc.settings = replace(svc.settings, sensitive_read_packages=frozenset())
        return value

    monkeypatch.setattr(purchase_http, "observe", observe)
    with pytest.raises(PlayError):
        apply(svc, prepared)
    assert [entry[0] for entry in calls] == ["GET", "GET"]


def test_scope_revocation_after_write_prevents_readback_but_preserves_acceptance(service):
    svc, _, calls, state = service
    prepared = prepare(svc)
    state["post_hook"] = lambda: setattr(
        svc, "settings", replace(svc.settings, sensitive_read_packages=frozenset())
    )
    result = apply(svc, prepared)
    assert result["mutation_succeeded"] is True
    assert result["readback_succeeded"] is False
    assert [entry[0] for entry in calls] == ["GET", "GET", "POST"]
    assert_private(result)


@pytest.mark.parametrize("field,new_value", [("quantity", 2), ("acknowledgementState", 1)])
def test_changed_economic_state_stops_before_write(service, field, new_value):
    svc, _, calls, state = service
    prepared = prepare(svc)
    state["resource"][field] = new_value
    with pytest.raises(PlayError):
        apply(svc, prepared)
    assert all(call[0] == "GET" for call in calls)


def test_missing_optional_product_identity_does_not_block_valid_route_bound_purchase(service):
    svc, _, _, state = service
    state["resource"].pop("productId")
    state["resource"].pop("purchaseToken")
    assert apply(svc, prepare(svc))["mutation_succeeded"] is True


def test_expired_operation_is_consumed_without_provider_io(service, monkeypatch):
    svc, _, calls, _ = service
    prepared = prepare(svc)
    expiry = svc._preparations[prepared["operation_id"]].deadline
    monkeypatch.setattr(purchase.time, "monotonic", lambda: expiry)
    with pytest.raises(PlayError, match="expired"):
        apply(svc, prepared)
    assert len(calls) == 1
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)


def test_expiry_during_preflight_stops_before_write(service, monkeypatch):
    svc, _, calls, _ = service
    prepared = prepare(svc)
    p = svc._preparations[prepared["operation_id"]]
    original_observe = purchase_http.observe

    def observe(*args, **kwargs):
        value = original_observe(*args, **kwargs)
        p.deadline = 0
        return value

    monkeypatch.setattr(purchase_http, "observe", observe)
    with pytest.raises(PlayError, match="expired"):
        apply(svc, prepared)
    assert [entry[0] for entry in calls] == ["GET", "GET"]


def test_shared_apply_lock_consumes_competing_operation_without_io(service):
    svc, _, calls, _ = service
    prepared = prepare(svc)
    assert _APPLY_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(PlayError, match="in progress"):
            apply(svc, prepared)
    finally:
        _APPLY_LOCK.release()
    assert len(calls) == 1
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)


def test_simultaneous_replay_sends_only_one_mutation(service):
    svc, _, calls, _ = service
    prepared = prepare(svc)
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        try:
            return apply(svc, prepared)
        except PlayError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        values = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(value, dict) for value in values) == 1
    assert sum(entry[0] == "POST" for entry in calls) == 1


@pytest.mark.parametrize("mutation", [[], {"unexpected": TOKEN}, None])
def test_invalid_write_response_never_looks_like_retryable_failure(service, mutation):
    svc, _, calls, state = service
    prepared = prepare(svc)
    state["mutation"] = mutation
    with pytest.raises(PlayError, match="outcome unknown") as error:
        apply(svc, prepared)
    assert TOKEN not in str(error.value)
    assert sum(entry[0] == "POST" for entry in calls) == 1
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)


def test_failed_readback_does_not_hide_success_or_echo_provider_error(service):
    svc, _, _, state = service
    prepared = prepare(svc)
    state["post_hook"] = lambda: state.update(resource=PlayError(TOKEN + AUTH + ACCOUNT))
    result = apply(svc, prepared)
    assert result["mutation_succeeded"] is True
    assert result["readback_succeeded"] is False
    assert_private(result)


def test_validation_only_deferral_preserves_original_etag_and_never_reports_mutation(service):
    svc, _, calls, state = service
    state["resource"] = {
        "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        "etag": "reviewed-private-etag",
        "linkedPurchaseToken": TOKEN,
        "lineItems": [
            {
                "productId": "monthly",
                "expiryTime": "2099-01-01T00:00:00Z",
                "autoRenewingPlan": {"autoRenewEnabled": True},
            }
        ],
    }
    state["mutation"] = {
        "itemExpiryTimeDetails": [
            {
                "productId": "monthly",
                "expiryTime": "2099-01-02T00:00:00Z",
            }
        ]
    }
    body = {
        "deferralContext": {
            "etag": "reviewed-private-etag",
            "deferDuration": "86400s",
            "validateOnly": True,
        }
    }
    prepared = svc.prepare(
        PREFIX + "purchases.subscriptionsv2.defer",
        {"packageName": APP, "token": TOKEN},
        body,
        True,
    )
    result = apply(svc, prepared)
    assert calls[2][3] == body
    assert result["validation_only"] is True
    assert result["validation_succeeded"] is True
    assert result["mutation_succeeded"] is None
    assert_private(prepared)
    assert_private(result)


@pytest.mark.parametrize(
    "returned_products", [["unrelated"], ["monthly"], ["monthly", "addon", "extra"]]
)
def test_deferral_response_must_account_for_the_observed_subscription_items(
    service, returned_products
):
    svc, _, calls, state = service
    state["resource"] = {
        "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        "etag": "reviewed-etag",
        "lineItems": [
            {
                "productId": product,
                "expiryTime": "2099-01-01T00:00:00Z",
                "autoRenewingPlan": {"autoRenewEnabled": True},
            }
            for product in ("monthly", "addon")
        ],
    }
    state["mutation"] = {
        "itemExpiryTimeDetails": [
            {
                "productId": product,
                "expiryTime": "2099-01-02T00:00:00Z",
            }
            for product in returned_products
        ]
    }
    prepared = svc.prepare(
        PREFIX + "purchases.subscriptionsv2.defer",
        {"packageName": APP, "token": TOKEN},
        {"deferralContext": {"etag": "reviewed-etag", "deferDuration": "86400s"}},
        True,
    )
    with pytest.raises(PlayError, match="outcome unknown"):
        apply(svc, prepared)
    assert [entry[0] for entry in calls] == ["GET", "GET", "POST"]


@pytest.mark.parametrize("changed", ["original_amount", "currency"])
def test_external_refund_response_cannot_change_the_observed_original_payment(service, changed):
    svc, _, _, state = service
    state["resource"] = {
        "packageName": APP,
        "externalTransactionId": "external-1",
        "transactionState": "TRANSACTION_REPORTED",
        "originalPreTaxAmount": {"currency": "EUR", "priceMicros": "1000000"},
        "originalTaxAmount": {"currency": "EUR", "priceMicros": "0"},
        "currentPreTaxAmount": {"currency": "EUR", "priceMicros": "1000000"},
        "currentTaxAmount": {"currency": "EUR", "priceMicros": "0"},
    }
    state["mutation"] = deepcopy(state["resource"])
    state["mutation"]["currentPreTaxAmount"]["priceMicros"] = "500000"
    if changed == "original_amount":
        state["mutation"]["originalPreTaxAmount"]["priceMicros"] = "2000000"
    else:
        for key in (
            "originalPreTaxAmount",
            "originalTaxAmount",
            "currentPreTaxAmount",
            "currentTaxAmount",
        ):
            state["mutation"][key]["currency"] = "USD"
    prepared = svc.prepare(
        PREFIX + "externaltransactions.refundexternaltransaction",
        {"name": "applications/" + APP + "/externalTransactions/external-1"},
        {
            "refundTime": "2026-01-01T00:00:00Z",
            "partialRefund": {
                "refundId": "refund-1",
                "refundPreTaxAmount": {"currency": "EUR", "priceMicros": "500000"},
            },
        },
        True,
    )
    with pytest.raises(PlayError, match="outcome unknown"):
        apply(svc, prepared)


def test_external_create_unavailable_baseline_does_not_prove_absence(service):
    svc, _, calls, state = service
    state["resource"] = None
    body = {
        "originalPreTaxAmount": {"currency": "EUR", "priceMicros": "1000000"},
        "originalTaxAmount": {"currency": "EUR", "priceMicros": "0"},
        "transactionTime": "2026-01-01T00:00:00Z",
        "userTaxAddress": {"regionCode": "PT"},
        "oneTimeTransaction": {"externalTransactionToken": TOKEN},
    }
    returned = {
        **body,
        "packageName": APP,
        "externalTransactionId": "external-1",
        "transactionState": "TRANSACTION_REPORTED",
        "currentPreTaxAmount": body["originalPreTaxAmount"],
        "currentTaxAmount": body["originalTaxAmount"],
    }
    state["mutation"] = returned
    state["post_hook"] = lambda: state.update(resource=deepcopy(returned))
    prepared = svc.prepare(
        PREFIX + "externaltransactions.createexternaltransaction",
        {"parent": "applications/" + APP, "externalTransactionId": "external-1"},
        body,
        True,
    )
    assert prepared["before"]["absence_verified"] is False
    result = apply(svc, prepared)
    assert [entry[0] for entry in calls] == ["GET", "GET", "POST", "GET"]
    assert result["stale_check_performed"] is False
    assert result["readback_succeeded"] is True
    assert_private(prepared)
    assert_private(result)


def test_real_mcp_preparation_redacts_sensitive_input(service, monkeypatch):
    _, _, _, _ = service
    monkeypatch.setattr("pubship.auth.Auth.token", lambda self, scope: AUTH)

    async def run():
        async with Client(create_server(configured())) as client:
            result = await client.call_tool(
                "prepare_purchase_operation",
                {
                    "method": ACK,
                    "parameters": PARAMETERS,
                    "body": BODY,
                    "acknowledge_live_effects": True,
                },
            )
            assert not result.is_error
            assert_private(result.structured_content)
            assert "purchase_" in json.dumps(result.structured_content)

    asyncio.run(run())
