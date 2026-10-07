"""Independent hosted 0.19 expiry, disclosure and aggregate-retention boundaries."""

import asyncio
import io
import json
import logging
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp import Client
from test_hosted_019_engine_hooks import catalog, migration_spec
from test_hosted_019_registry import P, prepare, scenario_for
from test_hosted_edits import PACKAGE, Provider, principal
from test_hosted_tools import hosted_tools as hosted_tools  # noqa: F401

from pubship import hosted_edits, purchase_http, support_http
from pubship.errors import PlayError
from pubship.hosted_edits import HostedEditRegistry
from pubship.hosted_grants import READ_SCOPE

CANARY_TOKEN = "SYNTHETIC_PRIVATE_PURCHASE_TOKEN"
CANARY_PAYLOAD = "SYNTHETIC_PRIVATE_DEVELOPER_PAYLOAD"
CANARY_ACCOUNT = "SYNTHETIC_PRIVATE_CUSTOMER_ACCOUNT"
CANARY_ERROR = "SYNTHETIC_PRIVATE_PROVIDER_ERROR"
PRIVATE = (CANARY_TOKEN, CANARY_PAYLOAD, CANARY_ACCOUNT, CANARY_ERROR)


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open",
        Mock(side_effect=AssertionError("Independent QA forbids live provider traffic")),
    )


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def registry(provider):
    value = HostedEditRegistry(provider)
    yield value
    value.close()


def migration_scenario(monkeypatch, products=("first", "second", "third"), padding=0):
    spec = migration_spec([(name, "base") for name in products])
    spec["parameters"]["packageName"] = PACKAGE
    for row in spec["body"]["requests"]:
        row["packageName"] = PACKAGE
    state = SimpleNamespace(padding=padding)
    reads, writes = [], []

    def send(url, token, **kwargs):
        if kwargs.get("method") == "POST":
            writes.append((url, token))
            return {"responses": [{} for _ in products]}
        name = url.rsplit("/", 1)[-1]
        reads.append((name, token))
        data = catalog(name, padding=state.padding)
        data["packageName"] = PACKAGE
        return data

    monkeypatch.setattr(purchase_http, "_send", send)
    return SimpleNamespace(
        method=spec["method"],
        parameters=spec["parameters"],
        body=spec["body"],
        kind="purchase",
        capability="play:subscriber-price-migration",
        version=None,
        reads=reads,
        writes=writes,
        state=state,
    )


@pytest.mark.parametrize("phase", ["prepare", "apply", "readback"])
@pytest.mark.parametrize("overrun", [0, 1], ids=["at-deadline", "after-deadline"])
@pytest.mark.parametrize(
    "family,target_get",
    [("support", 0), ("purchase", 0), ("migration", 0), ("migration", 1), ("migration", 2)],
)
def test_original_access_deadline_expires_inside_each_internal_get_authorization(
    registry, provider, monkeypatch, family, target_get, phase, overrun
):
    scenario = (
        migration_scenario(monkeypatch)
        if family == "migration"
        else scenario_for(
            monkeypatch,
            P + ("reviews.reply" if family == "support" else "purchases.products.consume"),
        )
    )
    clock = [time.monotonic()]
    monkeypatch.setattr(
        hosted_edits, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    )
    original = principal()
    original.expires = time.time() + 5
    # The engine's own 600-second clock remains live; the registry's shorter
    # original access deadline must be enforced inside each transport callback.
    state = SimpleNamespace(active=phase == "prepare", in_get=False, get_index=0, deadline=None)
    transport = support_http if family == "support" else purchase_http
    name = "_observe_get" if family == "support" else "_get"
    original_get = getattr(transport, name)

    def marked_get(*args, **kwargs):
        state.in_get = True
        try:
            return original_get(*args, **kwargs)
        finally:
            state.in_get = False
            state.get_index += 1

    monkeypatch.setattr(transport, name, marked_get)
    authorize = provider.authorize_current

    def slow_authorize(who, capability=None, package=None):
        result = authorize(who, capability, package)
        if state.active and state.in_get and state.get_index == target_get:
            deadline = state.deadline or next(iter(registry._records.values())).deadline
            clock[0] = deadline + overrun
        return result

    monkeypatch.setattr(provider, "authorize_current", slow_authorize)
    if phase == "prepare":
        with pytest.raises(PlayError, match="expired"):
            prepare(registry, scenario, original)
        assert len(scenario.reads) == target_get
        assert not scenario.writes
    else:
        operation = prepare(registry, scenario, original)["operation_id"]
        state.deadline = registry._records[operation].deadline
        scenario.reads.clear()
        state.get_index = 0
        if phase == "readback":
            original_execute = transport.execute

            def execute(*args, **kwargs):
                result = original_execute(*args, **kwargs)
                state.active, state.get_index = True, 0
                scenario.reads.clear()
                return result

            monkeypatch.setattr(transport, "execute", execute)
            result = registry.apply(principal(), scenario.kind, operation, operation)
            assert result["mutation_succeeded"] and not result["readback_succeeded"]
            assert len(scenario.writes) == 1
        else:
            state.active = True
            with pytest.raises(PlayError, match="expired"):
                registry.apply(principal(), scenario.kind, operation, operation)
            assert not scenario.writes
        assert len(scenario.reads) == target_get
    assert not registry._records


def test_parent_cache_bound_stops_before_third_get_and_releases_registry_quota(
    provider, monkeypatch
):
    scenario = migration_scenario(monkeypatch, padding=140_000)
    provider.allowed = {READ_SCOPE: frozenset({PACKAGE}), scenario.capability: frozenset({PACKAGE})}
    registry = HostedEditRegistry(provider, max_total=1)
    try:
        with pytest.raises(PlayError, match="hosted preparation limit"):
            prepare(registry, scenario)
        assert [name for name, _ in scenario.reads] == ["first", "second"]
        assert not scenario.writes and not registry._records
        scenario.state.padding = 0
        scenario.reads.clear()
        operation = prepare(registry, scenario)["operation_id"]
        assert operation in registry._records
        assert [name for name, _ in scenario.reads] == ["first", "second", "third"]
    finally:
        registry.close()


@pytest.mark.parametrize("failure", [None, "prepare", "preflight", "mutation", "readback"])
def test_financial_only_sdk_calls_project_all_outputs_and_transport_errors(
    hosted_tools, monkeypatch, caplog, failure
):
    server, provider, registry, _ = hosted_tools
    provider.allowed = {
        READ_SCOPE: frozenset({PACKAGE}),
        "play:purchase-actions": frozenset({PACKAGE}),
    }
    caplog.set_level(logging.INFO)
    calls = []
    resource = {
        "productId": "coins",
        "purchaseToken": CANARY_TOKEN,
        "purchaseState": 0,
        "acknowledgementState": 0,
        "consumptionState": 0,
        "developerPayload": CANARY_PAYLOAD,
        "obfuscatedExternalAccountId": CANARY_ACCOUNT,
    }
    fail_index = {"prepare": 0, "preflight": 1, "mutation": 2, "readback": 3}.get(failure)

    def open_request(opener, request, timeout):
        index = len(calls)
        calls.append(request.method)
        if index == fail_index:
            raise urllib.error.HTTPError(
                request.full_url, 503, CANARY_ERROR, {}, io.BytesIO(CANARY_ERROR.encode())
            )
        response = Mock(status=200)
        response.read.return_value = json.dumps(
            {} if request.method == "POST" else resource
        ).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    monkeypatch.setattr("urllib.request.OpenerDirector.open", open_request)
    parameters = {"packageName": PACKAGE, "productId": "coins", "token": CANARY_TOKEN}
    public_results = []

    async def run():
        async with Client(server) as client:
            denied = await client.call_tool(
                "read_sensitive_publisher",
                {"method": P + "purchases.products.get", "parameters": parameters},
            )
            assert denied.is_error
            assert not calls
            provider.services.assert_not_called()
            public_results.append(denied)
            prepared = await client.call_tool(
                "prepare_purchase_operation",
                {
                    "method": P + "purchases.products.acknowledge",
                    "parameters": parameters,
                    "body": {"developerPayload": CANARY_PAYLOAD},
                    "acknowledge_live_effects": True,
                },
            )
            public_results.append(prepared)
            if failure == "prepare":
                assert prepared.is_error
                return
            assert not prepared.is_error, prepared.content
            operation = prepared.structured_content["operation_id"]
            applied = await client.call_tool(
                "apply_purchase_operation", {"operation_id": operation, "confirmation": operation}
            )
            public_results.append(applied)
            if failure in {"preflight", "mutation"}:
                assert applied.is_error
            else:
                assert not applied.is_error, applied.content
                assert applied.structured_content["mutation_succeeded"]
                assert applied.structured_content["readback_succeeded"] is (failure != "readback")
            replay = await client.call_tool(
                "apply_purchase_operation", {"operation_id": operation, "confirmation": operation}
            )
            public_results.append(replay)
            assert replay.is_error

    asyncio.run(run())
    assert len(calls) == (4 if fail_index is None else fail_index + 1)
    assert calls.count("POST") == (0 if failure in {"prepare", "preflight"} else 1)
    public = json.dumps([result.model_dump(mode="json") for result in public_results])
    for canary in (*PRIVATE, "original-synthetic-token"):
        assert canary not in public + caplog.text
    assert "https://androidpublisher.googleapis.com/" not in public
    assert not registry._records


@pytest.mark.parametrize("suffix", ["createexternaltransaction", "refundexternaltransaction"])
@pytest.mark.parametrize(
    "path",
    [
        "applications/com.example.app%2f..%2fcom.example.foreign",
        "applications/com.example.app/../com.example.foreign",
        "applications/com.example.app?packageName=com.example.foreign",
        "applications/com.example.app#com.example.foreign",
        "applications//com.example.app",
    ],
)
def test_external_encoded_or_ambiguous_package_paths_never_authenticate(
    registry, provider, monkeypatch, suffix, path
):
    scenario = scenario_for(monkeypatch, P + "externaltransactions." + suffix)
    key = "parent" if suffix == "createexternaltransaction" else "name"
    scenario.parameters[key] = path if key == "parent" else path + "/externalTransactions/payment1"
    with pytest.raises(PlayError):
        prepare(registry, scenario)
    provider.services.assert_not_called()
    assert not scenario.reads and not scenario.writes and not registry._records


@pytest.mark.parametrize("overrun", [0, 1])
def test_no_baseline_declaration_final_authorization_cannot_outlive_original_access(
    registry, provider, monkeypatch, overrun
):
    scenario = scenario_for(monkeypatch, P + "applications.dataSafety")
    clock = [time.monotonic()]
    monkeypatch.setattr(
        hosted_edits, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    )
    owner = principal()
    owner.expires = time.time() + 5
    operation = prepare(registry, scenario, owner)["operation_id"]
    record = registry._records[operation]
    original_check = support_http.check_preflight
    authorize = provider.authorize_current
    final_check = [False]

    def preflight(*args):
        original_check(*args)
        final_check[0] = True

    def authorize_after_preflight(*args):
        authorization = authorize(*args)
        if final_check[0]:
            clock[0] = record.deadline + overrun
        return authorization

    monkeypatch.setattr(support_http, "check_preflight", preflight)
    monkeypatch.setattr(provider, "authorize_current", authorize_after_preflight)
    with pytest.raises(PlayError, match="expired"):
        registry.apply(principal(), scenario.kind, operation, operation)
    assert not scenario.reads and not scenario.writes and not registry._records
