"""Synthetic dispatch, retention and observation boundaries for the 0.19 engines."""

import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pubship import provider_transport, purchase, purchase_http, support, support_http
from pubship.config import Settings
from pubship.coverage import PURCHASE_ACTION_METHODS, SUPPORT_METHODS
from pubship.errors import PlayError
from pubship.publishing import _APPLY_LOCK
from pubship.purchase_contracts import request_spec

APP = "com.example.hosted"
AUTH = "synthetic-original-credential"
PRIVATE = "synthetic-private-purchase"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", Mock(side_effect=AssertionError("No live traffic"))
    )


@pytest.fixture(params=["support", "purchase"])
def engine(request, monkeypatch):
    module = support if request.param == "support" else purchase
    transport = support_http if request.param == "support" else purchase_http
    clock = [1000.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(transport, "time", fake_time)
    monkeypatch.setattr(provider_transport, "time", fake_time)
    state = SimpleNamespace(revoked=False, on_authorize=None, on_get=None, on_post=None)
    reads, writes = [], []

    def authorize():
        if state.on_authorize:
            state.on_authorize()
        if state.revoked:
            raise PlayError("Synthetic authority revoked")

    auth = Mock(token=Mock(return_value=AUTH))
    settings = Settings(
        packages=frozenset({APP}),
        support_packages=frozenset({APP}),
        support_methods=SUPPORT_METHODS,
        sensitive_read_packages=frozenset({APP}),
        purchase_packages=frozenset({APP}),
        purchase_methods=PURCHASE_ACTION_METHODS,
    )
    if request.param == "support":
        cls = support.SupportOperations
        method = "androidpublisher.reviews.reply"
        parameters = {"packageName": APP, "reviewId": "review1"}
        body = {"replyText": "Thanks"}
        before = {
            "reviewId": "review1",
            "comments": [{"userComment": {"text": "Review", "lastModified": {"seconds": "1"}}}],
        }
        mutation = {"result": {"replyText": "Thanks", "lastEdited": {"seconds": "2"}}}
    else:
        cls = purchase.PurchaseLifecycle
        method = "androidpublisher.purchases.products.consume"
        parameters = {"packageName": APP, "productId": "coins", "token": PRIVATE}
        body = None
        before = {
            "productId": "coins",
            "purchaseState": 0,
            "acknowledgementState": 0,
            "consumptionState": 0,
            "purchaseToken": PRIVATE,
        }
        mutation = {}

    def send(url, token, *, method="GET", body=None, **kwargs):
        if method == "POST" or body is not None:
            writes.append((url, token, body))
            if state.on_post:
                state.on_post()
            return deepcopy(mutation)
        reads.append((url, token))
        if state.on_get:
            state.on_get()
        return deepcopy(before)

    monkeypatch.setattr(transport, "_request" if module is support else "_send", send)
    svc = cls(
        settings,
        auth,
        _execution_authorization=authorize,
        _apply_guard=threading.Lock(),
        _max_snapshot_bytes=256 * 1024,
    )
    return SimpleNamespace(
        service=svc,
        cls=cls,
        module=module,
        transport=transport,
        settings=settings,
        method=method,
        parameters=parameters,
        body=body,
        before=before,
        mutation=mutation,
        auth=auth,
        state=state,
        clock=clock,
        reads=reads,
        writes=writes,
    )


def prepare(engine):
    return engine.service.prepare(engine.method, engine.parameters, engine.body, True)[
        "operation_id"
    ]


def test_hook_is_required_and_original_credential_survives_apply(engine):
    with pytest.raises(PlayError, match="local"):
        engine.cls(engine.settings, engine.auth).prepare(
            engine.method, engine.parameters, engine.body, True
        )
    operation = prepare(engine)
    engine.auth.token.return_value = "synthetic-replacement"
    result = engine.service.apply(operation, operation)
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    engine.auth.token.assert_called_once_with("publisher")
    assert {token for _, token in engine.reads} == {AUTH}
    assert engine.writes[0][1] == AUTH
    assert AUTH not in json.dumps(result)
    if engine.module is purchase:
        assert PRIVATE not in json.dumps(result)
    with pytest.raises(PlayError, match="consumed"):
        engine.service.apply(operation, operation)


@pytest.mark.parametrize("phase", ["credential", "authorization"])
@pytest.mark.parametrize("overrun", [0, 1])
def test_expiry_during_credential_or_authorization_stops_first_read(engine, phase, overrun):
    def expire():
        engine.clock[0] = 1600 + overrun

    def token(_):
        if phase == "credential":
            expire()
        else:
            engine.state.on_authorize = expire
        return AUTH

    engine.auth.token.side_effect = token
    with pytest.raises(PlayError, match="expired"):
        prepare(engine)
    assert not engine.reads and not engine.writes and not engine.service._preparations


def test_revocation_during_credentials_stops_first_read(engine):
    def token(_):
        engine.state.revoked = True
        return AUTH

    engine.auth.token.side_effect = token
    with pytest.raises(PlayError, match="revoked"):
        prepare(engine)
    assert not engine.reads and not engine.service._preparations


def test_revocation_during_prepare_read_prevents_retention(engine):
    engine.state.on_get = lambda: setattr(engine.state, "revoked", True)
    with pytest.raises(PlayError, match="revoked"):
        prepare(engine)
    assert len(engine.reads) == 1 and not engine.service._preparations


@pytest.mark.parametrize("reason", ["revoked", "expired"])
def test_final_apply_hook_blocks_mutation_and_consumes_id(engine, reason):
    operation = prepare(engine)

    def before_final_hook():
        if reason == "revoked":
            engine.state.revoked = True
        else:
            engine.state.on_authorize = lambda: engine.clock.__setitem__(0, 1600)

    engine.state.on_get = before_final_hook
    with pytest.raises(PlayError, match=reason):
        engine.service.apply(operation, operation)
    assert not engine.writes and not engine.service._preparations
    assert engine.service._apply_guard.acquire(blocking=False)
    engine.service._apply_guard.release()


@pytest.mark.parametrize("reason", ["revoked", "expired"])
def test_readback_denial_preserves_confirmed_mutation(engine, reason):
    operation = prepare(engine)

    def post():
        if reason == "revoked":
            engine.state.revoked = True
        else:
            engine.state.on_authorize = lambda: engine.clock.__setitem__(0, 1600)

    engine.state.on_post = post
    result = engine.service.apply(operation, operation)
    assert result["mutation_succeeded"] and not result["readback_succeeded"]
    assert len(engine.writes) == 1 and len(engine.reads) == 2
    assert "Synthetic authority" not in json.dumps(result)
    assert not engine.service._preparations


def test_hosted_apply_guard_is_separate_from_local_default(engine):
    operation = prepare(engine)
    assert engine.cls(engine.settings, engine.auth, local=True)._apply_guard is _APPLY_LOCK
    assert _APPLY_LOCK.acquire(blocking=False)
    try:
        assert engine.service.apply(operation, operation)["mutation_succeeded"]
    finally:
        _APPLY_LOCK.release()


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, 2 * 1024 * 1024 + 1])
def test_snapshot_limit_rejects_invalid_or_wider_values(engine, limit):
    with pytest.raises(PlayError, match="Snapshot limit"):
        engine.cls(engine.settings, engine.auth, _max_snapshot_bytes=limit)


def test_hosted_observation_bound_applies_before_retention(engine):
    engine.before["unexpectedPrivateField"] = "x" * (256 * 1024)
    with pytest.raises(PlayError, match="hosted preparation limit"):
        prepare(engine)
    assert len(engine.reads) == 1 and not engine.writes and not engine.service._preparations


def test_oversized_readback_preserves_confirmed_mutation(engine):
    operation = prepare(engine)
    engine.state.on_post = lambda: engine.before.update(unexpectedPrivateField="x" * (256 * 1024))
    result = engine.service.apply(operation, operation)
    assert result["mutation_succeeded"] and not result["readback_succeeded"]
    assert len(engine.writes) == 1 and len(engine.reads) == 3
    assert "unexpectedPrivateField" not in json.dumps(result)
    assert not engine.service._preparations


@pytest.mark.parametrize("engine", ["support"], indirect=True)
def test_no_baseline_support_still_has_final_mutation_authorization(engine, monkeypatch):
    engine.method = "androidpublisher.applications.dataSafety"
    engine.parameters, engine.body = {"packageName": APP}, {"safetyLabels": "operator csv"}
    operation = prepare(engine)
    original = support_http.check_preflight

    def preflight(*args):
        original(*args)
        engine.state.revoked = True

    monkeypatch.setattr(support_http, "check_preflight", preflight)
    with pytest.raises(PlayError, match="revoked"):
        engine.service.apply(operation, operation)
    assert not engine.reads and not engine.writes and not engine.service._preparations


@pytest.mark.parametrize("overrun", [0, 1])
def test_transport_checks_deadline_after_authorization_before_get(engine, overrun):
    spec = engine.transport.request_spec(engine.method, engine.parameters, engine.body)

    def authorize():
        engine.clock[0] = 1600 + overrun

    with pytest.raises(PlayError, match="expired"):
        engine.transport.observe(spec, AUTH, deadline=1600, _execution_authorization=authorize)
    assert not engine.reads


def migration_spec(targets):
    method = "androidpublisher.monetization.subscriptions.basePlans.batchMigratePrices"
    requests = [
        {
            "packageName": APP,
            "productId": product,
            "basePlanId": plan,
            "regionsVersion": {"version": "2022/02"},
            "regionalPriceMigrations": [
                {
                    "regionCode": "US",
                    "oldestAllowedPriceVersionTime": "2025-01-01T00:00:00Z",
                    "priceIncreaseType": "PRICE_INCREASE_TYPE_OPT_IN",
                }
            ],
        }
        for product, plan in targets
    ]
    return request_spec(method, {"packageName": APP, "productId": "-"}, {"requests": requests})


def catalog(product, plans=("base",), padding=0):
    return {
        "packageName": APP,
        "productId": product,
        "basePlans": [
            {
                "basePlanId": plan,
                "state": "ACTIVE",
                "autoRenewingBasePlanType": {"billingPeriodDuration": "P1M"},
                "regionalConfigs": [
                    {"regionCode": "US", "price": {"currencyCode": "USD", "units": "10"}}
                ],
            }
            for plan in plans
        ],
        "listings": [{"languageCode": "en-US", "title": "x" * padding}],
    }


@pytest.mark.parametrize("reason", ["revoked", "expired"])
def test_migration_reauthorizes_between_distinct_parent_gets(monkeypatch, reason):
    spec = migration_spec([("first", "base"), ("second", "base")])
    calls, clock = [], [1000]
    monkeypatch.setattr(purchase_http, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def read(url, token, **kwargs):
        calls.append(url)
        return catalog("first")

    def authorize():
        if calls:
            if reason == "revoked":
                raise PlayError("Synthetic authority revoked")
            clock[0] = 1600

    monkeypatch.setattr(purchase_http, "_send", read)
    with pytest.raises(PlayError, match=reason):
        purchase_http.observe(spec, AUTH, deadline=1600, _execution_authorization=authorize)
    assert len(calls) == 1 and calls[0].endswith("/first")


def test_migration_hosted_bound_counts_cached_parents_before_next_read(monkeypatch):
    spec = migration_spec([(product, "base") for product in ("first", "second", "third")])
    calls = []

    def read(url, token, **kwargs):
        product = url.rsplit("/", 1)[-1]
        calls.append(product)
        return catalog(product, padding=1600)

    monkeypatch.setattr(purchase_http, "_send", read)
    with pytest.raises(PlayError, match="hosted preparation limit"):
        purchase_http.observe(spec, AUTH, _max_snapshot_bytes=3000)
    assert calls == ["first", "second"]
    # The same observations remain valid under the unchanged local limit.
    assert len(purchase_http.observe(spec, AUTH)["targets"]) == 3


def test_migration_hosted_bound_counts_selected_target_copies(monkeypatch):
    spec = migration_spec([("first", plan) for plan in ("base", "second", "third")])
    data = catalog("first", ("base", "second", "third"))
    selected = {
        "packageName": APP,
        "productId": "first",
        "basePlanId": "base",
        "basePlan": data["basePlans"][0],
    }
    limit = len(purchase_http._bounded([{"first": data}, [selected]])) + 10
    read = Mock(return_value=data)
    monkeypatch.setattr(purchase_http, "_send", read)
    with pytest.raises(PlayError, match="hosted preparation limit"):
        purchase_http.observe(spec, AUTH, _max_snapshot_bytes=limit)
    read.assert_called_once()


def test_support_local_fingerprint_limit_remains_two_mib():
    spec = {"method": "androidpublisher.applications.dataSafety"}
    data = {"padding": "x" * (512 * 1024)}
    assert len(support_http.fingerprint(spec, data)) > 512 * 1024
    with pytest.raises(PlayError, match="hosted preparation limit"):
        support_http.fingerprint(spec, data, max_bytes=256 * 1024)
