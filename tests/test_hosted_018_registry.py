"""Hosted 0.18 method authority, shared lifetime and isolation; synthetic provider I/O."""

import json
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_edit_writes_adversarial import CASES as EDIT_CASES
from test_hosted_edits import PACKAGE, Provider, principal
from test_legacy_products import service as legacy_service
from test_monetization import case as commerce_case
from test_monetization import service as subscription_service
from test_onetime_commerce import service as onetime_service

from pubship import edit_writes_http, hosted_edits, http, monetization_http
from pubship.errors import PlayError
from pubship.hosted_catalog import (
    HOSTED_COMMERCE_CAPABILITIES,
    HOSTED_COMMERCE_QUERY_METHODS,
    HOSTED_EDIT_RESOURCE_METHODS,
)
from pubship.hosted_edits import MAX_OPERATION_BYTES, HostedEditRegistry
from pubship.hosted_grants import READ_SCOPE
from pubship.monetization_contracts import observations, request_spec

EDIT = "androidpublisher.edits."
SUB = "androidpublisher.monetization.subscriptions."
ONETIME = "androidpublisher.monetization.onetimeproducts."
LEGACY = "androidpublisher.inappproducts."
EXPECTED_EDIT = {
    EDIT + suffix
    for suffix in (
        "details.patch",
        "details.update",
        "expansionfiles.patch",
        "expansionfiles.update",
        "images.delete",
        "images.deleteall",
        "listings.delete",
        "listings.deleteall",
        "listings.update",
        "tracks.create",
        "tracks.patch",
    )
}
EXPECTED_COMMERCE = {
    **dict.fromkeys(
        [
            SUB + suffix
            for suffix in (
                "create",
                "patch",
                "batchUpdate",
                "delete",
                "basePlans.activate",
                "basePlans.deactivate",
                "basePlans.delete",
                "basePlans.batchUpdateStates",
                "basePlans.offers.create",
                "basePlans.offers.patch",
                "basePlans.offers.delete",
                "basePlans.offers.activate",
                "basePlans.offers.deactivate",
                "basePlans.offers.batchUpdate",
                "basePlans.offers.batchUpdateStates",
            )
        ],
        "play:subscription-catalog",
    ),
    **dict.fromkeys(
        [
            ONETIME + suffix
            for suffix in (
                "patch",
                "delete",
                "batchUpdate",
                "batchDelete",
                "purchaseOptions.batchDelete",
                "purchaseOptions.batchUpdateStates",
                "purchaseOptions.offers.activate",
                "purchaseOptions.offers.deactivate",
                "purchaseOptions.offers.cancel",
                "purchaseOptions.offers.batchDelete",
                "purchaseOptions.offers.batchUpdate",
                "purchaseOptions.offers.batchUpdateStates",
            )
        ],
        "play:onetime-catalog",
    ),
    **dict.fromkeys(
        [
            LEGACY + suffix
            for suffix in ("insert", "patch", "update", "delete", "batchUpdate", "batchDelete")
        ],
        "play:legacy-product-catalog",
    ),
}
WRITE_METHODS = sorted(EXPECTED_EDIT | EXPECTED_COMMERCE.keys())
REPRESENTATIVES = [EDIT + "details.patch", SUB + "patch", ONETIME + "patch", LEGACY + "update"]


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def registry(provider):
    value = HostedEditRegistry(provider)
    yield value
    value.close()


def setup_operation(monkeypatch, method, *, upsert=False):
    """Reuse provider fixtures, never a local engine for the hosted invocation."""
    if method in EXPECTED_EDIT:
        _, _, extra, body, _, before, response, after = EDIT_CASES[method.removeprefix(EDIT)]
        parameters = {"packageName": PACKAGE, "editId": "owned-edit", **extra}
        reads, writes = [], []
        edit = {"id": "owned-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}

        def get(url, token):
            reads.append((url, token))
            if url.endswith("/edits/owned-edit"):
                return deepcopy(edit)
            return deepcopy(after if writes else before)

        def execute(m, p, b, token, **kwargs):
            writes.append((m, deepcopy(p), deepcopy(b), token))
            # The real HTTP adapter normalizes documented empty bodies to {}.
            return {} if response is None else deepcopy(response)

        monkeypatch.setattr(http, "get", get)
        monkeypatch.setattr(edit_writes_http, "execute", execute)
        capability, kind = "play:edit-resources", "edit_resource"
    else:
        factory = (
            legacy_service
            if method.startswith(LEGACY)
            else onetime_service
            if method.startswith(ONETIME)
            else subscription_service
        )
        args = {"upsert": upsert} if factory is not subscription_service else {}
        _, parameters, body, _, reads, writes, _ = factory(monkeypatch, method, **args)
        capability, kind = EXPECTED_COMMERCE[method], "commerce"
    return SimpleNamespace(
        method=method,
        parameters=deepcopy(parameters),
        body=deepcopy(body),
        reads=reads,
        writes=writes,
        capability=capability,
        kind=kind,
    )


def prepare(registry, scenario, who=None):
    return registry.prepare(
        who or principal(),
        scenario.kind,
        scenario.parameters,
        scenario.body,
        True,
        method=scenario.method,
    )


def test_exact_47_method_boundary_and_three_disjoint_commerce_capabilities():
    assert HOSTED_EDIT_RESOURCE_METHODS == EXPECTED_EDIT
    assert HOSTED_COMMERCE_CAPABILITIES == EXPECTED_COMMERCE
    assert HOSTED_COMMERCE_QUERY_METHODS == {
        "androidpublisher.monetization.convertRegionPrices",
        SUB + "basePlans.offers.batchGet",
        ONETIME + "purchaseOptions.offers.batchGet",
    }
    assert len(EXPECTED_EDIT) == 11 and len(EXPECTED_COMMERCE) == 33


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_every_write_keeps_exact_capability_original_credential_and_owner(
    registry, provider, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    provider.allowed = {READ_SCOPE: frozenset({PACKAGE}), scenario.capability: frozenset({PACKAGE})}
    expected_parameters, expected_body = deepcopy(scenario.parameters), deepcopy(scenario.body)
    prepared = prepare(registry, scenario)
    operation = prepared["operation_id"]
    record = registry._records[operation]
    assert record.method == method and record.capability == scenario.capability
    assert not scenario.writes
    scenario.parameters["packageName"] = "com.example.caller_mutation"
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
    assert applied["mutation_succeeded"] and applied["readback_succeeded"]
    assert scenario.writes == [
        (method, expected_parameters, expected_body, "original-synthetic-token")
    ]
    provider.auth.token.assert_called_once_with("publisher")
    assert "synthetic-token" not in json.dumps([prepared, applied])
    assert not registry._records
    with pytest.raises(PlayError, match="consumed"):
        registry.apply(principal(), scenario.kind, operation, operation)


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_each_method_requires_its_own_capability_before_provider_services(
    registry, provider, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    del provider.allowed[scenario.capability]
    with pytest.raises(PlayError, match="denied"):
        prepare(registry, scenario)
    provider.services.assert_not_called()
    assert not scenario.reads and not scenario.writes and not registry._records


@pytest.mark.parametrize(
    "kind,method",
    [
        ("edit_resource", EDIT + "apks.upload"),
        ("edit_resource", EDIT + "bundles.upload"),
        ("edit_resource", EDIT + "commit"),
        ("commerce", "androidpublisher.orders.refund"),
        ("commerce", SUB + "archive"),
        ("commerce", SUB + "basePlans.migratePrices"),
        ("commerce", EDIT + "details.patch"),
        ("edit_resource", SUB + "patch"),
        ("listing", SUB + "patch"),
        ("invalid", SUB + "patch"),
        ("commerce", None),
        ("edit_resource", []),
        ("commerce", {}),
    ],
)
def test_invalid_kind_type_and_excluded_method_never_reach_services(
    registry, provider, kind, method
):
    with pytest.raises(PlayError):
        registry.prepare(principal(), kind, {"packageName": PACKAGE}, {}, True, method=method)
    provider.services.assert_not_called()
    assert not registry._records


@pytest.mark.parametrize("method", [LEGACY + "update", LEGACY + "batchUpdate"])
def test_legacy_allow_missing_keeps_additional_insert_grant(
    registry, provider, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method, upsert=True)
    provider.allowed = {READ_SCOPE: frozenset({PACKAGE}), scenario.capability: frozenset({PACKAGE})}
    operation = prepare(registry, scenario)["operation_id"]
    methods = registry._records[operation].engine.settings.monetization_methods
    assert methods == frozenset(
        m for m, cap in EXPECTED_COMMERCE.items() if cap == scenario.capability
    )
    assert LEGACY + "insert" in methods
    del provider.allowed[scenario.capability]
    with pytest.raises(PlayError, match="denied"):
        registry.apply(principal(), scenario.kind, operation, operation)
    assert not scenario.writes


@pytest.mark.parametrize("method", REPRESENTATIVES)
@pytest.mark.parametrize("change", ["disconnect", "close", "expire"])
def test_new_families_erase_retained_credentials_on_invalidation(
    registry, provider, monkeypatch, method, change
):
    scenario = setup_operation(monkeypatch, method)
    operation = prepare(registry, scenario)["operation_id"]
    engine = registry._records[operation].engine
    if change == "disconnect":
        provider.disconnect("connection-a")
    elif change == "close":
        registry.close()
    else:
        registry._records[operation].deadline = 0
        registry.cleanup()
    assert not registry._records and not engine._preparations
    assert engine._execution_authorization.principal is None
    with pytest.raises(PlayError):
        registry.apply(principal(), scenario.kind, operation, operation)
    assert not scenario.writes


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_new_preparation_and_existing_listing_share_quota_and_apply_guard(
    provider, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    registry = HostedEditRegistry(provider, max_total=1)
    try:
        operation = prepare(registry, scenario)["operation_id"]
        provider.services.reset_mock()
        with pytest.raises(PlayError, match="capacity"):
            registry.prepare(
                principal(),
                "listing",
                {"packageName": PACKAGE, "editId": "owned-edit", "language": "en-US"},
                {"title": "new"},
            )
        provider.services.assert_not_called()
        assert (
            registry._records[operation].engine._apply_guard is registry._records[operation].guard
        )
        assert len(registry._records) == 1
    finally:
        registry.close()


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_lost_authority_after_confirmed_write_preserves_success(
    registry, provider, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    operation = prepare(registry, scenario)["operation_id"]
    module = edit_writes_http if scenario.kind == "edit_resource" else monetization_http
    original = module.execute

    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        del provider.allowed[scenario.capability]
        return result

    monkeypatch.setattr(module, "execute", execute)
    result = registry.apply(principal(), scenario.kind, operation, operation)
    assert result["mutation_succeeded"] and not result["readback_succeeded"]
    assert len(scenario.writes) == 1 and not registry._records


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_original_deadline_still_blocks_apply_with_refreshed_mcp_principal(
    registry, provider, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    clock = [time.monotonic()]
    monkeypatch.setattr(
        hosted_edits, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0])
    )
    owner = principal()
    owner.expires = time.time() + 5
    operation = prepare(registry, scenario, owner)["operation_id"]
    record = registry._records[operation]
    engine = record.engine
    authorize = provider.authorize_current

    def expire_on_consumption(who, capability=None, package=None):
        result = authorize(who, capability, package)
        if not engine._preparations:
            clock[0] = record.deadline
        return result

    monkeypatch.setattr(provider, "authorize_current", expire_on_consumption)
    with pytest.raises(PlayError, match="expired"):
        registry.apply(principal(), scenario.kind, operation, operation)
    assert not scenario.writes and not registry._records


@pytest.mark.parametrize("method", sorted(HOSTED_COMMERCE_QUERY_METHODS))
def test_query_requires_current_authority_after_credential_refresh(
    registry, provider, monkeypatch, method
):
    parameters, body = commerce_case(method)
    query = Mock(return_value={"data": {}})
    monkeypatch.setattr(monetization_http, "query", query)
    who = principal()

    def expire(service):
        who.expires = 0
        return "synthetic-token"

    provider.auth.token.side_effect = expire
    with pytest.raises(PlayError, match="expired"):
        registry.query_monetization(who, method, parameters, body)
    query.assert_not_called()
    assert not registry._records


def test_commerce_snapshot_bound_releases_reservation_before_next_operation(provider, monkeypatch):
    scenario = setup_operation(monkeypatch, SUB + "patch")
    observe = monetization_http.observe

    def oversized(*args):
        data = observe(*args)
        data["data"]["synthetic_extension"] = "x" * (256 * 1024)
        return data

    monkeypatch.setattr(monetization_http, "observe", oversized)
    registry = HostedEditRegistry(
        provider, max_total=1, max_payload_bytes_total=MAX_OPERATION_BYTES
    )
    try:
        with pytest.raises(PlayError, match="snapshot"):
            prepare(registry, scenario)
        assert not registry._records and not scenario.writes
        monkeypatch.setattr(monetization_http, "observe", observe)
        operation = prepare(registry, scenario)["operation_id"]
        assert registry.apply(principal(), scenario.kind, operation, operation)[
            "mutation_succeeded"
        ]
    finally:
        registry.close()


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_authority_loss_between_batch_observations_stops_the_next_target(
    registry, provider, monkeypatch, phase
):
    scenario = setup_operation(monkeypatch, SUB + "batchUpdate")
    second = deepcopy(scenario.body["requests"][0])
    second["subscription"]["productId"] = "second_product"
    scenario.body["requests"].append(second)
    spec = request_spec(scenario.method, scenario.parameters, scenario.body)
    expected = observations(spec)
    revoke = [phase == "prepare"]

    def observe(url, token):
        if revoke[0]:
            del provider.allowed[scenario.capability]
        return {"http_status": 200, "data": dict(expected[url]["identity"])}

    observation = Mock(side_effect=observe)
    monkeypatch.setattr(monetization_http, "observe", observation)
    if phase == "prepare":
        with pytest.raises(PlayError, match="denied"):
            prepare(registry, scenario)
    else:
        operation = prepare(registry, scenario)["operation_id"]
        assert observation.call_count == 2
        observation.reset_mock()
        revoke[0] = True
        with pytest.raises(PlayError, match="denied"):
            registry.apply(principal(), scenario.kind, operation, operation)
    observation.assert_called_once()
    assert not scenario.writes and not registry._records


@pytest.mark.parametrize(
    "method", [SUB + "batchUpdate", ONETIME + "batchUpdate", LEGACY + "batchUpdate"]
)
def test_mixed_parent_batch_identity_is_rejected_before_google_credentials(
    registry, provider, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    nested = scenario.body["requests"][0]
    resource = next(
        nested[key] for key in ("subscription", "oneTimeProduct", "inappproduct") if key in nested
    )
    resource["packageName"] = "com.example.ungranted"
    with pytest.raises(PlayError):
        prepare(registry, scenario)
    provider.auth.token.assert_not_called()
    assert not scenario.reads and not scenario.writes and not registry._records


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_new_and_old_families_share_one_connection_guard_but_not_other_connections(
    registry, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    operation = prepare(registry, scenario)["operation_id"]
    edit = {"id": "old-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    monkeypatch.setattr(
        http,
        "get",
        lambda url, token: (
            {"language": "en-US", "title": "old"} if "/listings/" in url else dict(edit)
        ),
    )
    parameters = {"packageName": PACKAGE, "editId": "old-edit", "language": "en-US"}
    old = registry.prepare(principal(), "listing", parameters, {"title": "new"})["operation_id"]
    other = registry.prepare(
        principal("second-connection"), "listing", parameters, {"title": "new"}
    )["operation_id"]
    assert registry._records[operation].guard is registry._records[old].guard
    assert registry._records[operation].guard is not registry._records[other].guard


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_unknown_provider_outcome_consumes_new_operation_without_readback_or_retry(
    registry, monkeypatch, method
):
    scenario = setup_operation(monkeypatch, method)
    operation = prepare(registry, scenario)["operation_id"]
    module = edit_writes_http if scenario.kind == "edit_resource" else monetization_http
    observed_at_dispatch = []

    def uncertain(*args, **kwargs):
        observed_at_dispatch.append(len(scenario.reads))
        raise PlayError("Synthetic uncertain mutation result; reconciliation required.")

    mutation = Mock(side_effect=uncertain)
    monkeypatch.setattr(module, "execute", mutation)
    with pytest.raises(PlayError, match="uncertain"):
        registry.apply(principal(), scenario.kind, operation, operation)
    assert observed_at_dispatch == [len(scenario.reads)]
    assert not registry._records
    with pytest.raises(PlayError, match="consumed"):
        registry.apply(principal(), scenario.kind, operation, operation)
    mutation.assert_called_once()
