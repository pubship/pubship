"""Synthetic commerce contracts, live-write boundaries and failure reconciliation."""

import asyncio
import io
import json
import urllib.error
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp import Client

from pubship import monetization_http
from pubship.catalog import list_methods
from pubship.config import Settings
from pubship.coverage import MONETIZATION_QUERY_METHODS, MONETIZATION_WRITE_METHODS
from pubship.errors import PlayError
from pubship.monetization import Monetization
from pubship.monetization_contracts import OFFER, PREFIX, SUB, observations, request_spec
from pubship.publishing import _APPLY_LOCK
from pubship.server import create_server

APP = "com.example.app"
P = {"packageName": APP, "productId": "premium"}
PLAN = {**P, "basePlanId": "monthly"}
OFFER_IDENTITY = {**PLAN, "offerId": "intro"}
LISTINGS = [{"languageCode": "en-US", "title": "Premium"}]
REGION = {"regionsVersion.version": "2022/02"}
# Region versions are opaque query values, not path segments.


def case(method):
    offer = ".offers." in method
    identity = OFFER_IDENTITY if offer else (PLAN if ".basePlans." in method else P)
    if method.endswith(".create"):
        resource = {
            **identity,
            **(
                {
                    "phases": [
                        {
                            "duration": "P1W",
                            "recurrenceCount": 1,
                            "regionalConfigs": [{"regionCode": "US", "free": {}}],
                        }
                    ],
                    "regionalConfigs": [{"regionCode": "US"}],
                }
                if offer
                else {"listings": LISTINGS}
            ),
        }
        return {**identity, **REGION}, resource
    if method.endswith(".patch"):
        field = "offerTags" if offer else "listings"
        value = [] if offer else LISTINGS
        return {**identity, **REGION, "updateMask": field}, {**identity, field: value}
    if method.endswith(".batchUpdate"):
        field, resource_field, value = (
            ("offerTags", "subscriptionOffer", [])
            if offer
            else ("listings", "subscription", LISTINGS)
        )
        return (
            {k: v for k, v in identity.items() if k != "offerId"} if offer else {"packageName": APP}
        ), {
            "requests": [
                {
                    resource_field: {**identity, field: value},
                    "updateMask": field,
                    "regionsVersion": {"version": REGION["regionsVersion.version"]},
                }
            ]
        }
    if method.endswith(".batchUpdateStates"):
        field = "activateSubscriptionOfferRequest" if offer else "activateBasePlanRequest"
        return {
            k: v for k, v in identity.items() if k not in ({"offerId"} if offer else {"basePlanId"})
        }, {"requests": [{field: identity}]}
    if method.endswith(".batchGet"):
        if ".onetimeproducts." in method:
            identity = {**P, "purchaseOptionId": "buy", "offerId": "discount"}
        return {k: v for k, v in identity.items() if k != "offerId"}, {"requests": [identity]}
    if method.endswith(".convertRegionPrices"):
        return {"packageName": APP}, {
            "price": {"currencyCode": "USD", "units": "5", "nanos": 990000000}
        }
    return deepcopy(identity), None if method.endswith(".delete") else {}


def service(monkeypatch, method=SUB + "patch"):
    auth_calls, reads, writes = [], [], []
    settings = Settings(
        packages=frozenset({APP}),
        monetization_packages=frozenset({APP}),
        monetization_methods=MONETIZATION_WRITE_METHODS,
    )
    auth = SimpleNamespace(token=lambda scope: auth_calls.append(scope) or "synthetic-token")
    svc = Monetization(settings, auth, local=True)
    params, body = deepcopy(case(method))
    spec = request_spec(method, params, body)
    snapshots = {}
    for url, expected in observations(spec).items():
        data = {**expected["identity"]}
        if "offerId" not in data:
            data.update(
                listings=deepcopy(LISTINGS), basePlans=[{"basePlanId": "monthly", "state": "DRAFT"}]
            )
        snapshots[url] = (
            {"http_status": 404, "data": None}
            if expected["create"]
            else {"http_status": 200, "data": data}
        )

    def observe(url, token):
        reads.append((url, token))
        return deepcopy(snapshots[url])

    def execute(m, p, b, token, **kwargs):
        writes.append((m, deepcopy(p), deepcopy(b), token))
        if m.endswith(".delete"):
            return {}
        rows = [
            dict(t) if ".offers." in m else {k: t[k] for k in ("packageName", "productId")}
            for t in spec["targets"]
        ]
        return (
            {"subscriptionOffers" if ".offers." in m else "subscriptions": rows}
            if m.endswith((".batchUpdate", ".batchUpdateStates"))
            else rows[0]
        )

    monkeypatch.setattr(monetization_http, "observe", observe)
    monkeypatch.setattr(monetization_http, "execute", execute)
    return svc, params, body, snapshots, reads, writes, auth_calls


@pytest.mark.parametrize(
    "method", sorted(m for m in MONETIZATION_WRITE_METHODS if m.startswith(SUB))
)
def test_every_write_prepare_apply_replay_and_original_credential(monkeypatch, method):
    svc, p, b, _, reads, writes, auth = service(monkeypatch, method)
    proposal = svc.prepare(method, p, b, True)
    assert not proposal["executed"] and not writes
    operation = proposal["operation_id"]
    result = svc.apply(operation, operation)
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert result["live_change"] and not result["availability_verified"]
    assert len(writes) == 1 and auth == ["publisher"]
    assert all(token == "synthetic-token" for _, token in reads)
    assert writes[0][-1] == "synthetic-token"
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(operation, operation)
    assert len(writes) == 1


@pytest.mark.parametrize("method", sorted(MONETIZATION_QUERY_METHODS))
def test_queries_are_scoped_before_auth(monkeypatch, method):
    calls = []
    svc = Monetization(
        Settings(packages=frozenset({APP})),
        SimpleNamespace(token=lambda _: calls.append("token") or "x"),
        local=True,
    )
    p, b = case(method)
    monkeypatch.setattr(
        monetization_http,
        "query",
        lambda *args: {
            "data": {"unknownFutureField": 1},
            "http_status": 200,
            "content_present": True,
        },
    )
    assert svc.query(method, p, b)["data"] == {"unknownFutureField": 1}
    p["packageName"] = "com.other.app"
    if "requests" in b:
        b["requests"][0]["packageName"] = p["packageName"]
    with pytest.raises(PlayError, match="outside"):
        svc.query(method, p, b)
    assert calls == ["token"]


@pytest.mark.parametrize("bad", ["*", "../bad", "UPPER", "", "a/b", "-", "%2f", "a\n"])
def test_invalid_nested_identity_never_authenticates(monkeypatch, bad):
    svc, p, b, _, _, writes, auth = service(monkeypatch, SUB + "batchUpdate")
    b["requests"][0]["subscription"]["productId"] = bad
    with pytest.raises(PlayError):
        svc.prepare(SUB + "batchUpdate", p, b, True)
    assert not writes and not auth


@pytest.mark.parametrize(
    "change", ["foreign", "duplicate", "empty", "oversize", "oneof_both", "oneof_none"]
)
def test_batch_bounds_and_oneof(monkeypatch, change):
    method = SUB + "basePlans.batchUpdateStates"
    svc, p, b, _, _, _, auth = service(monkeypatch, method)
    request = b["requests"][0]
    if change == "foreign":
        request["activateBasePlanRequest"]["packageName"] = "com.other.app"
    if change == "duplicate":
        b["requests"] *= 2
    if change == "empty":
        b["requests"] = []
    if change == "oversize":
        b["requests"] *= 101
    if change == "oneof_both":
        request["deactivateBasePlanRequest"] = request["activateBasePlanRequest"]
    if change == "oneof_none":
        b["requests"] = [{}]
    with pytest.raises(PlayError):
        svc.prepare(method, p, b, True)
    assert not auth


@pytest.mark.parametrize(
    "change",
    [
        "mask_missing",
        "mask_wildcard",
        "mask_duplicate",
        "mask_identity",
        "mask_omitted_body",
        "unmasked",
        "allowMissing",
        "region_missing",
        "output_only",
        "body_identity",
    ],
)
def test_explicit_patch_intent(monkeypatch, change):
    method = SUB + "patch"
    svc, p, b, _, _, writes, auth = service(monkeypatch, method)
    if change == "mask_missing":
        p.pop("updateMask")
    if change == "mask_wildcard":
        p["updateMask"] = "*"
    if change == "mask_duplicate":
        p["updateMask"] = "listings,listings"
    if change == "mask_identity":
        p["updateMask"] = "productId"
    if change == "mask_omitted_body":
        b.pop("listings")
    if change == "unmasked":
        b["basePlans"] = []
    if change == "allowMissing":
        p["allowMissing"] = True
    if change == "region_missing":
        p.pop("regionsVersion.version")
    if change == "output_only":
        b["archived"] = False
    if change == "body_identity":
        b["packageName"] = "com.other.app"
    with pytest.raises(PlayError):
        svc.prepare(method, p, b, True)
    assert not auth and not writes


@pytest.mark.parametrize("mode", ["hosted", "no_app", "no_method", "no_ack"])
def test_independent_live_gates(monkeypatch, mode):
    svc, p, b, _, _, writes, auth = service(monkeypatch)
    if mode == "hosted":
        svc.local = False
    if mode == "no_app":
        svc.settings = Settings(
            packages=frozenset({APP}), monetization_methods=MONETIZATION_WRITE_METHODS
        )
    if mode == "no_method":
        svc.settings = Settings(packages=frozenset({APP}), monetization_packages=frozenset({APP}))
    with pytest.raises(PlayError):
        svc.prepare(SUB + "patch", p, b, mode != "no_ack")
    assert not auth and not writes


@pytest.mark.parametrize(
    "mode",
    [
        "drift",
        "expired",
        "lock",
        "missing",
        "bad_identity",
        "unknown",
        "bad_response",
        "partial_response",
    ],
)
def test_apply_failures_consume_once_and_release_lock(monkeypatch, mode):
    method = SUB + "batchUpdate" if mode == "partial_response" else SUB + "patch"
    svc, p, b, snapshots, _, writes, _ = service(monkeypatch, method)
    op = svc.prepare(method, p, b, True)["operation_id"]
    if mode == "drift":
        next(iter(snapshots.values()))["data"]["newField"] = "changed"
    if mode == "expired":
        svc._preparations[op].deadline = 0
    if mode == "lock":
        assert _APPLY_LOCK.acquire(blocking=False)
    if mode == "missing":
        snapshots[next(iter(snapshots))] = {"http_status": 404, "data": None}
    if mode == "bad_identity":
        next(iter(snapshots.values()))["data"]["packageName"] = "com.other.app"
    if mode == "unknown":
        monkeypatch.setattr(
            monetization_http,
            "execute",
            lambda *a: (_ for _ in ()).throw(PlayError(monetization_http.UNKNOWN_OUTCOME)),
        )
    if mode == "bad_response":
        monkeypatch.setattr(monetization_http, "execute", lambda *a: {"productId": "foreign"})
    if mode == "partial_response":
        monkeypatch.setattr(monetization_http, "execute", lambda *a: {"subscriptions": []})
    try:
        with pytest.raises(PlayError):
            svc.apply(op, op)
    finally:
        if mode == "lock":
            _APPLY_LOCK.release()
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(op, op)
    assert not writes
    assert _APPLY_LOCK.acquire(blocking=False)
    _APPLY_LOCK.release()


def test_readback_failure_preserves_mutation_success(monkeypatch):
    svc, p, b, _, _, writes, _ = service(monkeypatch)
    op = svc.prepare(SUB + "patch", p, b, True)["operation_id"]
    original = monetization_http.observe
    monkeypatch.setattr(
        monetization_http,
        "observe",
        lambda *a: (_ for _ in ()).throw(PlayError("read failed")) if writes else original(*a),
    )
    result = svc.apply(op, op)
    assert result["mutation_succeeded"] and not result["readback_succeeded"]
    assert "consumed" in result["observation_error"]


def test_preview_mutation_and_auth_rotation_do_not_change_frozen_intent(monkeypatch):
    svc, p, b, _, _, writes, _ = service(monkeypatch)
    result = svc.prepare(SUB + "patch", p, b, True)
    result["proposed_request"]["body"]["listings"][0]["title"] = "tampered preview"
    b["listings"][0]["title"] = "tampered input"
    svc.auth = SimpleNamespace(token=lambda _: pytest.fail("must use original credential"))
    op = result["operation_id"]
    svc.apply(op, op)
    assert writes[0][2]["listings"][0]["title"] == "Premium"


@pytest.mark.parametrize("status,data", [(200, b'{"convertedRegionPrices":{}}'), (204, b"")])
def test_actual_query_transport_is_post_and_preserves_no_content(monkeypatch, status, data):
    sent = []

    class Response(io.BytesIO):
        pass

    response = Response(data)
    response.status = status

    def open_request(req, timeout):
        sent.append(req)
        return response

    monkeypatch.setattr(
        monetization_http.urllib.request,
        "build_opener",
        lambda *a: Mock(handlers=[], open=open_request),
    )
    p, b = case(PREFIX + "convertRegionPrices")
    result = monetization_http.query(PREFIX + "convertRegionPrices", p, b, "synthetic")
    assert sent[0].method == "POST" and json.loads(sent[0].data) == b
    assert result["content_present"] == (status == 200)
    assert sent[0].full_url.endswith("/pricing:convertRegionPrices")


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 409, 429, 500])
def test_http_failure_never_retries_or_leaks_provider_body(monkeypatch, status):
    calls = []

    def fail(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(
            req.full_url, status, "private-title", {}, io.BytesIO(b"private response")
        )

    monkeypatch.setattr(
        monetization_http.urllib.request, "build_opener", lambda *a: Mock(handlers=[], open=fail)
    )
    p, b = case(SUB + "patch")
    with pytest.raises(PlayError, match="outcome unknown") as error:
        monetization_http.execute(SUB + "patch", p, b, "private-token")
    assert len(calls) == 1 and "private" not in str(error.value) and APP not in str(error.value)


def test_404_is_typed_observation_not_proof_of_absence(monkeypatch):
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "private", {}, io.BytesIO())

    monkeypatch.setattr(
        monetization_http.urllib.request, "build_opener", lambda *a: Mock(handlers=[], open=fail)
    )
    assert monetization_http.observe("https://androidpublisher.googleapis.com/anything", "x") == {
        "http_status": 404,
        "data": None,
    }


def test_unsupported_archive_has_no_tool_or_coming_soon_promise():
    rows = list_methods(status="provider_unsupported")["methods"]
    assert [r["id"] for r in rows] == [SUB + "archive"] and rows[0]["tool"] is None
    with pytest.raises(PlayError):
        request_spec(SUB + "archive", P, {})


def test_mcp_registry_local_only_and_annotations():
    async def check():
        settings = Settings(
            packages=frozenset({APP}),
            monetization_packages=frozenset({APP}),
            monetization_methods=MONETIZATION_WRITE_METHODS,
        )
        async with Client(create_server(settings)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert tools["query_monetization"].annotations.read_only_hint
            assert tools["prepare_monetization_write"].annotations.read_only_hint
            assert tools["apply_monetization_write"].annotations.destructive_hint
            bad = await client.call_tool(
                "prepare_monetization_write",
                {"method": SUB + "delete", "parameters": P, "acknowledge_live_effects": "true"},
            )
            assert bad.is_error
        async with Client(
            create_server(
                service_factory=lambda: pytest.fail("hosted credentials must not resolve")
            )
        ) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert not names & {
                "query_monetization",
                "prepare_monetization_write",
                "apply_monetization_write",
            }

    asyncio.run(check())


@pytest.mark.parametrize(
    "content,status", [(b"{}", 204), (b"[]", 200), (b'{"x": NaN}', 200), (b"", 200), (b"{}", 302)]
)
def test_invalid_transport_responses_are_uncertain_after_dispatch(monkeypatch, content, status):
    class Response(io.BytesIO):
        pass

    response = Response(content)
    response.status = status
    calls = []

    def open_request(req, timeout):
        calls.append(req)
        return response

    monkeypatch.setattr(
        monetization_http.urllib.request,
        "build_opener",
        lambda *a: Mock(handlers=[], open=open_request),
    )
    p, b = case(SUB + "patch")
    with pytest.raises(PlayError, match="outcome unknown"):
        monetization_http.execute(SUB + "patch", p, b, "synthetic")
    assert len(calls) == 1


@pytest.mark.parametrize("status,content", [(204, b""), (200, b""), (200, b"{}")])
def test_delete_accepts_only_documented_empty_success(monkeypatch, status, content):
    class Response(io.BytesIO):
        pass

    response = Response(content)
    response.status = status
    monkeypatch.setattr(
        monetization_http.urllib.request,
        "build_opener",
        lambda *a: Mock(handlers=[], open=lambda *a, **k: response),
    )
    p, b = case(SUB + "delete")
    assert monetization_http.execute(SUB + "delete", p, b, "synthetic") == {}


@pytest.mark.parametrize(
    "mode", ["foreign_nested", "output_only", "missing_plan", "missing_offer", "output_only_nested"]
)
def test_offer_and_plan_specific_boundaries(monkeypatch, mode):
    method = OFFER + "create" if mode == "foreign_nested" else OFFER + "patch"
    svc, p, b, snapshots, _, writes, auth = service(monkeypatch, method)
    if mode == "foreign_nested":
        b["basePlanId"] = "foreign"
    if mode == "output_only":
        b["state"] = "ACTIVE"
    if mode == "missing_plan":
        next(o["data"] for o in snapshots.values() if o["data"] and "basePlans" in o["data"])[
            "basePlans"
        ] = []
    if mode == "missing_offer":
        key = next(k for k in snapshots if "/offers/" in k)
        snapshots[key] = {"http_status": 404, "data": None}
    if mode == "output_only_nested":
        method = SUB + "patch"
        p = {**P, **REGION, "updateMask": "basePlans"}
        b = {
            **P,
            "basePlans": [
                {
                    "basePlanId": "monthly",
                    "state": "ACTIVE",
                    "autoRenewingBasePlanType": {"billingPeriodDuration": "P1M"},
                }
            ],
        }
    with pytest.raises(PlayError):
        svc.prepare(method, p, b, True)
    assert not writes
    if mode not in {"missing_plan", "missing_offer"}:
        assert not auth


def test_batch_wildcards_allow_only_explicit_inner_targets():
    method = OFFER + "batchGet"
    p, b = case(method)
    p.update(productId="-", basePlanId="-")
    assert request_spec(method, p, b, query=True)["targets"] == [OFFER_IDENTITY]
    b["requests"][0]["productId"] = "-"
    with pytest.raises(PlayError):
        request_spec(method, p, b, query=True)


def test_regions_version_is_opaque_and_query_encoded():
    p, b = case(SUB + "patch")
    p["regionsVersion.version"] = "2022/02"
    assert "regionsVersion.version=2022%2F02" in request_spec(SUB + "patch", p, b)["url"]


def test_resource_snapshot_and_capacity_limits(monkeypatch):
    import pubship.monetization as module

    svc, p, b, snapshots, _, writes, _ = service(monkeypatch)
    monkeypatch.setattr(module, "MAX_PREPARATIONS", 1)
    svc.prepare(SUB + "patch", p, b, True)
    with pytest.raises(PlayError, match="capacity"):
        svc.prepare(SUB + "patch", p, b, True)
    svc._preparations.clear()
    monkeypatch.setattr(module, "MAX_SNAPSHOT_BYTES", 10)
    with pytest.raises(PlayError, match="snapshot"):
        svc.prepare(SUB + "patch", p, b, True)
    assert not writes and not svc._preparations


def test_expiry_during_baseline_and_wrong_confirmation(monkeypatch):
    import pubship.monetization as module

    svc, p, b, _, _, writes, _ = service(monkeypatch)
    op = svc.prepare(SUB + "patch", p, b, True)["operation_id"]
    with pytest.raises(PlayError, match="Confirmation"):
        svc.apply(op, "wrong")
    assert op in svc._preparations
    original = monetization_http.observe

    def slow(*args):
        result = original(*args)
        svc._preparations.clear()
        monkeypatch.setattr(module.time, "monotonic", lambda: float("inf"))
        return result

    monkeypatch.setattr(monetization_http, "observe", slow)
    with pytest.raises(PlayError, match="expired"):
        svc.apply(op, op)
    assert not writes


def test_real_stdio_commerce_prepare_apply_and_replay(tmp_path):
    import sys

    from mcp.client.stdio import StdioServerParameters

    script = tmp_path / "synthetic_commerce.py"
    script.write_text("""
from pubship import monetization_http
from pubship.auth import Auth
from pubship.config import Settings
from pubship.coverage import MONETIZATION_WRITE_METHODS
from pubship.server import create_server
APP="com.example.app"
Auth.token=lambda self,scope:"synthetic-stdio-token"
monetization_http.observe=lambda url,token:{"http_status":200,"data":{"packageName":APP,"productId":"premium","listings":[{"languageCode":"en-US","title":"Old"}]}}
monetization_http.execute=lambda method,parameters,body,token:dict(body)
monetization_http.query=lambda *args:{"http_status":204,"content_present":False,"data":None}
p=frozenset({APP})
create_server(Settings(packages=p,monetization_packages=p,monetization_methods=MONETIZATION_WRITE_METHODS)).run()
""")

    async def check():
        async with Client(
            StdioServerParameters(command=sys.executable, args=[str(script)])
        ) as client:
            p, b = case(SUB + "patch")
            result = await client.call_tool(
                "prepare_monetization_write",
                {
                    "method": SUB + "patch",
                    "parameters": p,
                    "body": b,
                    "acknowledge_live_effects": True,
                },
            )
            assert not result.is_error, result.content
            op = result.structured_content["operation_id"]
            applied = await client.call_tool(
                "apply_monetization_write", {"operation_id": op, "confirmation": op}
            )
            assert not applied.is_error and applied.structured_content["mutation_succeeded"]
            replay = await client.call_tool(
                "apply_monetization_write", {"operation_id": op, "confirmation": op}
            )
            assert replay.is_error
            p, b = case(PREFIX + "convertRegionPrices")
            read = await client.call_tool(
                "query_monetization",
                {"method": PREFIX + "convertRegionPrices", "parameters": p, "body": b},
            )
            assert not read.is_error and read.structured_content["data"] is None

    asyncio.run(check())


def test_expired_readback_preserves_confirmed_write(monkeypatch):
    svc, p, b, _, _, writes, _ = service(monkeypatch)
    op = svc.prepare(SUB + "patch", p, b, True)["operation_id"]
    preparation = svc._preparations[op]
    original = monetization_http.execute

    def expires_after_write(*args):
        result = original(*args)
        preparation.deadline = 0
        return result

    monkeypatch.setattr(monetization_http, "execute", expires_after_write)
    result = svc.apply(op, op)
    assert len(writes) == 1
    assert result["mutation_succeeded"] and not result["readback_succeeded"]


def test_caller_mutation_during_preparation_cannot_change_preview(monkeypatch):
    svc, p, b, _, _, writes, _ = service(monkeypatch)
    original = monetization_http.observe

    def mutate_caller(*args):
        result = original(*args)
        b["listings"][0]["title"] = "changed outside tool"
        return result

    monkeypatch.setattr(monetization_http, "observe", mutate_caller)
    preview = svc.prepare(SUB + "patch", p, b, True)
    assert preview["proposed_request"]["body"]["listings"][0]["title"] == "Premium"
    op = preview["operation_id"]
    svc.apply(op, op)
    assert writes[0][2]["listings"][0]["title"] == "Premium"


@pytest.mark.parametrize("unknown_operation", [False, True])
def test_apply_releases_other_expired_credentials_without_discarding_live_preparations(
    monkeypatch, unknown_operation
):
    svc, p, b, _, _, writes, auth = service(monkeypatch)
    expired = svc.prepare(SUB + "patch", p, b, True)["operation_id"]
    selected = svc.prepare(SUB + "patch", p, b, True)["operation_id"]
    untouched = svc.prepare(SUB + "patch", p, b, True)["operation_id"]
    svc._preparations[expired].deadline = 0
    if unknown_operation:
        unknown = "commerce_" + "a" * 43
        assert unknown not in svc._preparations
        with pytest.raises(PlayError, match="Unknown"):
            svc.apply(unknown, unknown)
        assert selected in svc._preparations and not writes
    else:
        assert svc.apply(selected, selected)["mutation_succeeded"]
        assert selected not in svc._preparations and len(writes) == 1
    assert expired not in svc._preparations
    assert untouched in svc._preparations
    assert len(auth) == 3
