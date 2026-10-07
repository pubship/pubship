"""0.19 hosted operation authority and lifecycle, with synthetic provider responses."""

import json
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_edit_writes_adversarial import CASES as EDIT_CASES
from test_hosted_edits import PACKAGE, Provider, principal
from test_purchase_transport import CASES as PURCHASE_CASES
from test_purchase_transport import catalog, external, order, product, subscription
from test_support_transport import case as support_case

from pubship import edit_writes_http, http, purchase_http, support_http
from pubship.errors import PlayError
from pubship.hosted_catalog import (
    HOSTED_PURCHASE_CAPABILITIES,
    HOSTED_SUPPORT_CAPABILITIES,
    HOSTED_TESTER_WRITE_METHODS,
)
from pubship.hosted_edits import HostedEditRegistry
from pubship.hosted_grants import READ_SCOPE

P = "androidpublisher."
EXPECTED = {
    **dict.fromkeys([P + "reviews.reply"], "play:review-replies"),
    **dict.fromkeys([P + "applications.dataSafety"], "play:app-declarations"),
    **dict.fromkeys(
        [
            P + name
            for name in (
                "applications.deviceTierConfigs.create",
                "systemapks.variants.create",
                "apprecovery.create",
                "apprecovery.addTargeting",
                "apprecovery.cancel",
                "apprecovery.deploy",
            )
        ],
        "play:app-operations",
    ),
    **dict.fromkeys(
        [P + "edits.testers." + name for name in ("patch", "update")], "play:tester-audience"
    ),
    **dict.fromkeys(
        [
            P + name
            for name in (
                "purchases.products.acknowledge",
                "purchases.products.consume",
                "purchases.subscriptions.acknowledge",
                "purchases.subscriptions.cancel",
                "purchases.subscriptions.defer",
                "purchases.subscriptionsv2.cancel",
                "purchases.subscriptionsv2.defer",
                "purchases.subscriptionsv2.revoke",
                "orders.refund",
                "orders.reviewrefund",
            )
        ],
        "play:purchase-actions",
    ),
    **dict.fromkeys(
        [
            P + "externaltransactions." + name
            for name in (
                "createexternaltransaction",
                "refundexternaltransaction",
            )
        ],
        "play:external-transactions",
    ),
    **dict.fromkeys(
        [
            P + "monetization.subscriptions.basePlans." + name
            for name in (
                "migratePrices",
                "batchMigratePrices",
            )
        ],
        "play:subscriber-price-migration",
    ),
}
METHODS = sorted(EXPECTED)
REPRESENTATIVES = [
    P + name
    for name in (
        "reviews.reply",
        "applications.dataSafety",
        "apprecovery.deploy",
        "edits.testers.patch",
        "orders.refund",
        "externaltransactions.createexternaltransaction",
        "monetization.subscriptions.basePlans.migratePrices",
    )
]


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def registry(provider):
    value = HostedEditRegistry(provider)
    yield value
    value.close()


def scenario_for(monkeypatch, method):
    name = method.removeprefix(P)
    reads, writes = [], []
    version = None
    if method in HOSTED_SUPPORT_CAPABILITIES:
        parameters, body, mutation, before, after, version = support_case(name)
        kind = "support"

        def request(url, token, **kwargs):
            if "body" in kwargs:
                writes.append((method, deepcopy(parameters), deepcopy(kwargs["body"]), token))
                return deepcopy(mutation)
            reads.append((url, token))
            return deepcopy(after if writes else before)

        monkeypatch.setattr(support_http, "_request", request)
    elif method in HOSTED_TESTER_WRITE_METHODS:
        _, _, extra, body, _, before, mutation, after = EDIT_CASES[name.removeprefix("edits.")]
        parameters = {"packageName": PACKAGE, "editId": "owned-edit", **extra}
        kind = "edit_resource"
        edit = {"id": "owned-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}

        def get(url, token):
            reads.append((url, token))
            if url.endswith("/edits/owned-edit"):
                return deepcopy(edit)
            return deepcopy(after if writes else before)

        def execute(m, p, b, token, **kwargs):
            writes.append((m, deepcopy(p), deepcopy(b), token))
            return deepcopy(mutation)

        monkeypatch.setattr(http, "get", get)
        monkeypatch.setattr(edit_writes_http, "execute", execute)
    else:
        _, parameters, body, mutation = next(case for case in PURCHASE_CASES if case[0] == name)
        kind = "purchase"

        def send(url, token, **kwargs):
            if kwargs.get("method") == "POST":
                writes.append((method, deepcopy(parameters), deepcopy(kwargs.get("body")), token))
                return deepcopy(mutation)
            reads.append((url, token))
            if name.startswith("externaltransactions"):
                if writes:
                    return deepcopy(mutation)
                return None if name.endswith("createexternaltransaction") else external()
            if name.startswith("monetization"):
                return catalog()
            if name.startswith("orders"):
                return order()
            if name.startswith("purchases.products."):
                return product()
            return subscription()

        monkeypatch.setattr(purchase_http, "_send", send)
    return SimpleNamespace(
        method=method,
        parameters=deepcopy(parameters),
        body=deepcopy(body),
        kind=kind,
        capability=EXPECTED[method],
        version=version,
        reads=reads,
        writes=writes,
    )


def prepare(registry, scenario, who=None):
    return registry.prepare(
        who or principal(),
        scenario.kind,
        scenario.parameters,
        scenario.body,
        True,
        method=scenario.method,
        observation_version_code=scenario.version,
    )


def test_exact_24_write_methods_and_disjoint_capabilities():
    assert EXPECTED == {
        **HOSTED_SUPPORT_CAPABILITIES,
        **HOSTED_PURCHASE_CAPABILITIES,
        **dict.fromkeys(HOSTED_TESTER_WRITE_METHODS, "play:tester-audience"),
    }
    assert len(EXPECTED) == 24


@pytest.mark.parametrize("method", METHODS)
def test_all_writes_use_exact_authority_retained_credentials_and_single_use(
    registry,
    provider,
    monkeypatch,
    method,
):
    scenario = scenario_for(monkeypatch, method)
    provider.allowed = {READ_SCOPE: frozenset({PACKAGE}), scenario.capability: frozenset({PACKAGE})}
    prepared = prepare(registry, scenario)
    operation = prepared["operation_id"]
    record = registry._records[operation]
    assert record.package == PACKAGE and record.method == method
    assert record.capability == scenario.capability and not scenario.writes
    scenario.parameters.clear()
    if scenario.body is not None:
        scenario.body.clear()
    provider.auth.token.return_value = "changed-synthetic-token"
    for thief in (principal("other-connection"), principal(client="other-client")):
        with pytest.raises(PlayError, match="Unknown"):
            registry.apply(thief, scenario.kind, operation, operation)
    with pytest.raises(PlayError, match="Unknown"):
        registry.apply(principal(), "listing", operation, operation)
    assert registry._records[operation] is record and not scenario.writes
    applied = registry.apply(principal(), scenario.kind, operation, operation)
    assert applied["mutation_succeeded"]
    assert len(scenario.writes) == 1 and scenario.writes[0][-1] == "original-synthetic-token"
    provider.auth.token.assert_called_once_with("publisher")
    public = json.dumps([prepared, applied])
    assert "synthetic-token" not in public
    if scenario.kind == "purchase":
        for private in (
            "private-purchase-sentinel",
            "external-token-sentinel",
            "refund-token-sentinel",
            "address-sentinel",
            "account-sentinel",
            "payload-sentinel",
        ):
            assert private not in public
    assert not registry._records
    with pytest.raises(PlayError, match="consumed"):
        registry.apply(principal(), scenario.kind, operation, operation)


@pytest.mark.parametrize("method", METHODS)
def test_every_write_denies_missing_capability_before_services(
    registry, provider, monkeypatch, method
):
    scenario = scenario_for(monkeypatch, method)
    del provider.allowed[scenario.capability]
    with pytest.raises(PlayError, match="denied"):
        prepare(registry, scenario)
    provider.services.assert_not_called()
    assert not scenario.reads and not scenario.writes and not registry._records


@pytest.mark.parametrize("method", METHODS)
def test_every_write_denies_authority_lost_during_credential_acquisition(
    registry, provider, monkeypatch, method
):
    scenario = scenario_for(monkeypatch, method)

    def revoke(service):
        del provider.allowed[scenario.capability]
        return "original-synthetic-token"

    provider.auth.token.side_effect = revoke
    with pytest.raises(PlayError, match="denied"):
        prepare(registry, scenario)
    assert not scenario.reads and not scenario.writes and not registry._records


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_shared_quota_and_cleanup_release_new_family_credentials(provider, monkeypatch, method):
    scenario = scenario_for(monkeypatch, method)
    registry = HostedEditRegistry(provider, max_total=1)
    try:
        operation = prepare(registry, scenario)["operation_id"]
        engine = registry._records[operation].engine
        provider.services.reset_mock()
        with pytest.raises(PlayError, match="capacity"):
            registry.prepare(principal(), "listing", {"packageName": PACKAGE}, {"title": "new"})
        provider.services.assert_not_called()
        provider.disconnect("connection-a")
        assert not registry._records and not engine._preparations
        assert engine._execution_authorization.principal is None
    finally:
        registry.close()


@pytest.mark.parametrize(
    "method",
    [
        P + "externaltransactions." + action
        for action in ("createexternaltransaction", "refundexternaltransaction")
    ],
)
@pytest.mark.parametrize("confusion", ["foreign", "spoof", "path"])
def test_external_package_is_extracted_from_validated_parent_not_caller_claim(
    registry,
    provider,
    monkeypatch,
    method,
    confusion,
):
    scenario = scenario_for(monkeypatch, method)
    key = "parent" if "parent" in scenario.parameters else "name"
    if confusion == "foreign":
        scenario.parameters[key] = scenario.parameters[key].replace(PACKAGE, "com.example.foreign")
    elif confusion == "spoof":
        scenario.parameters["packageName"] = PACKAGE
        scenario.parameters[key] = scenario.parameters[key].replace(PACKAGE, "com.example.foreign")
    else:
        scenario.parameters[key] += "/../com.example.foreign"
    with pytest.raises(PlayError):
        prepare(registry, scenario)
    provider.services.assert_not_called()
    assert not scenario.reads and not scenario.writes and not registry._records


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_earliest_deadline_blocks_refreshed_principal(registry, provider, monkeypatch, method):
    scenario = scenario_for(monkeypatch, method)
    operation = prepare(registry, scenario)["operation_id"]
    registry._records[operation].deadline = time.monotonic()
    with pytest.raises(PlayError, match="expired"):
        registry.apply(principal(), scenario.kind, operation, operation)
    assert not scenario.writes and not registry._records


@pytest.mark.parametrize(
    "method", [P + "reviews.reply", P + "orders.refund", P + "edits.testers.patch"]
)
def test_confirmed_mutation_survives_revoked_readback(registry, provider, monkeypatch, method):
    scenario = scenario_for(monkeypatch, method)
    operation = prepare(registry, scenario)["operation_id"]
    module = {
        "support": support_http,
        "purchase": purchase_http,
        "edit_resource": edit_writes_http,
    }[scenario.kind]
    original = module.execute

    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        del provider.allowed[scenario.capability]
        return result

    monkeypatch.setattr(module, "execute", execute)
    result = registry.apply(principal(), scenario.kind, operation, operation)
    assert result["mutation_succeeded"] and not result["readback_succeeded"]
    assert len(scenario.writes) == 1 and not registry._records


@pytest.mark.parametrize(
    "kind,method",
    [
        ("commerce", P + "orders.refund"),
        ("purchase", P + "reviews.reply"),
        ("support", P + "orders.refund"),
        ("purchase", P + "monetization.subscriptions.archive"),
    ],
)
def test_no_cross_family_or_unsupported_dispatch(registry, provider, kind, method):
    with pytest.raises(PlayError):
        registry.prepare(principal(), kind, {"packageName": PACKAGE}, {}, True, method=method)
    provider.services.assert_not_called()


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_unknown_mutation_is_consumed_without_retry_or_readback(registry, monkeypatch, method):
    scenario = scenario_for(monkeypatch, method)
    operation = prepare(registry, scenario)["operation_id"]
    module = {
        "support": support_http,
        "purchase": purchase_http,
        "edit_resource": edit_writes_http,
    }[scenario.kind]
    reads_at_dispatch = []

    def uncertain(*args, **kwargs):
        reads_at_dispatch.append(len(scenario.reads))
        raise PlayError("Synthetic uncertain outcome; reconcile before retrying.")

    mutation = Mock(side_effect=uncertain)
    monkeypatch.setattr(module, "execute", mutation)
    with pytest.raises(PlayError, match="uncertain"):
        registry.apply(principal(), scenario.kind, operation, operation)
    assert reads_at_dispatch == [len(scenario.reads)]
    assert not registry._records
    with pytest.raises(PlayError, match="consumed"):
        registry.apply(principal(), scenario.kind, operation, operation)
    mutation.assert_called_once()


def test_mixed_package_migration_rejected_before_services(registry, provider, monkeypatch):
    scenario = scenario_for(
        monkeypatch, P + "monetization.subscriptions.basePlans.batchMigratePrices"
    )
    scenario.body["requests"][0]["packageName"] = "com.example.foreign"
    with pytest.raises(PlayError):
        prepare(registry, scenario)
    provider.services.assert_not_called()
    assert not scenario.reads and not scenario.writes and not registry._records


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_large_request_rejected_before_services(registry, provider, monkeypatch, method):
    scenario = scenario_for(monkeypatch, method)
    scenario.body = {"oversized": "x" * (256 * 1024)}
    with pytest.raises(PlayError, match="256 KiB"):
        prepare(registry, scenario)
    provider.services.assert_not_called()
    assert not scenario.reads and not scenario.writes and not registry._records


def test_recovery_observation_version_is_retained_and_not_sent_as_mutation(registry, monkeypatch):
    scenario = scenario_for(monkeypatch, P + "apprecovery.deploy")
    operation = prepare(registry, scenario)["operation_id"]
    record = registry._records[operation]
    retained = record.engine._preparations[record.inner_id]
    assert retained.observation_version_code == "42"
    assert any("versionCode=42" in url for url, _ in scenario.reads)
    registry.apply(principal(), scenario.kind, operation, operation)
    assert "observation_version_code" not in json.dumps(scenario.writes)
