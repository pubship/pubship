"""Adversarial modern product/option/offer coverage without live mutations."""

import io
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pubship import monetization_http
from pubship.config import Settings
from pubship.coverage import ONETIME_WRITE_METHODS
from pubship.errors import PlayError
from pubship.monetization import Monetization
from pubship.monetization_contracts import observations, request_spec
from pubship.onetime_contracts import PREFIX

APP = "com.example.app"
PRODUCT = {"packageName": APP, "productId": "premium"}
OPTION = {**PRODUCT, "purchaseOptionId": "lifetime"}
OFFER = {**OPTION, "offerId": "sale"}
REGION = {"regionsVersion.version": "2026/01"}
LISTINGS = [{"languageCode": "en-US", "title": "Premium", "description": "Thank you"}]


def case(method, upsert=False):
    is_offer = ".offers." in method
    is_option = ".purchaseOptions." in method
    identity = OFFER if is_offer else OPTION if is_option else PRODUCT
    if method.endswith(".patch"):
        body = {**PRODUCT, "listings": deepcopy(LISTINGS)}
        mask = "listings"
        if upsert:
            body["purchaseOptions"] = [{"purchaseOptionId": "lifetime", "buyOption": {}}]
            mask += ",purchaseOptions"
        return {**PRODUCT, **REGION, "updateMask": mask, "allowMissing": upsert}, body
    if method.endswith(".batchUpdate"):
        if is_offer:
            resource = {**OFFER, "offerTags": []}
            mask = "offerTags"
            if upsert:
                resource["discountedOffer"] = {}
                mask += ",discountedOffer"
        else:
            _, resource = case(PREFIX + "patch", upsert)
            mask = "listings,purchaseOptions" if upsert else "listings"
        item = {
            "oneTimeProductOffer" if is_offer else "oneTimeProduct": resource,
            "updateMask": mask,
            "regionsVersion": {"version": "2026/01"},
            "allowMissing": upsert,
        }
    elif method.endswith(".batchUpdateStates"):
        item = {
            "activateOneTimeProductOfferRequest"
            if is_offer
            else "activatePurchaseOptionRequest": dict(identity)
        }
    else:
        item = dict(identity)
    if ".batch" in method:
        parent = {
            k: v
            for k, v in identity.items()
            if k
            not in (
                {"offerId"} if is_offer else {"purchaseOptionId"} if is_option else {"productId"}
            )
        }
        return parent, {"requests": [item]}
    return dict(identity), None if method.endswith(".delete") else {}


def service(monkeypatch, method, upsert=False):
    calls, auth, writes = [], [], []
    settings = Settings(
        packages=frozenset({APP}),
        monetization_packages=frozenset({APP}),
        monetization_methods=ONETIME_WRITE_METHODS,
    )
    svc = Monetization(
        settings,
        SimpleNamespace(token=lambda scope: auth.append(scope) or "synthetic-original"),
        local=True,
    )
    p, b = case(method, upsert)
    spec = request_spec(method, p, b)
    snapshots = {}
    for key, expected in observations(spec).items():
        data = deepcopy(expected["identity"])
        if expected["offer"]:
            data = {"oneTimeProductOffers": [data]}
        else:
            data["purchaseOptions"] = [{"purchaseOptionId": "lifetime", "state": "ACTIVE"}]
        snapshots[key] = {"http_status": 200, "data": data}

    def observe(url, token):
        calls.append(("GET", url, token))
        return deepcopy(snapshots[url])

    def observe_query(method, parameters, body, token):
        calls.append(("POST", deepcopy(body), token))
        target = body["requests"][0]
        key = next(
            key
            for key, expected in observations(spec).items()
            if expected["offer"] and expected["identity"] == target
        )
        return deepcopy(snapshots[key])

    def execute(method, parameters, body, token, **kwargs):
        writes.append((method, deepcopy(parameters), deepcopy(body), token))
        if spec["empty_response"]:
            return {}
        rows = [
            dict(t) if ".offers." in method else {k: t[k] for k in ("packageName", "productId")}
            for t in spec["targets"]
        ]
        return (
            {"oneTimeProductOffers" if ".offers." in method else "oneTimeProducts": rows}
            if ".batch" in method
            else rows[0]
        )

    monkeypatch.setattr(monetization_http, "observe", observe)
    monkeypatch.setattr(monetization_http, "observe_query", observe_query)
    monkeypatch.setattr(monetization_http, "execute", execute)
    return svc, p, b, snapshots, calls, writes, auth


@pytest.mark.parametrize("method", sorted(ONETIME_WRITE_METHODS))
def test_every_modern_write_review_apply_original_credential_and_replay(monkeypatch, method):
    svc, p, b, _, calls, writes, auth = service(monkeypatch, method)
    preview = svc.prepare(method, p, b, True)
    assert not writes and preview["effects"]
    op = preview["operation_id"]
    result = svc.apply(op, op)
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert len(writes) == 1 and auth == ["publisher"]
    assert all(call[-1] == "synthetic-original" for call in calls)
    assert writes[0][-1] == "synthetic-original"
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(op, op)


@pytest.mark.parametrize(
    "method",
    [PREFIX + "patch", PREFIX + "batchUpdate", PREFIX + "purchaseOptions.offers.batchUpdate"],
)
@pytest.mark.parametrize("unavailable", [False, True])
def test_explicit_upsert_freezes_qualified_observation(monkeypatch, method, unavailable):
    svc, p, b, snapshots, _, writes, _ = service(monkeypatch, method, True)
    if unavailable:
        for key, expected in observations(request_spec(method, p, b)).items():
            if expected["upsert"]:
                snapshots[key] = {"http_status": 404, "data": None}
    preview = svc.prepare(method, p, b, True)
    assert "ignores the update mask" in preview["effects"]
    op = preview["operation_id"]
    assert svc.apply(op, op)["mutation_succeeded"]
    assert len(writes) == 1


@pytest.mark.parametrize("representation", [{}, {"oneTimeProductOffers": []}])
def test_omitted_offer_is_not_an_empty_existing_target(monkeypatch, representation):
    method = PREFIX + "purchaseOptions.offers.batchUpdate"
    svc, p, b, snapshots, _, writes, _ = service(monkeypatch, method)
    key = next(
        key
        for key, expected in observations(request_spec(method, p, b)).items()
        if expected["offer"]
    )
    snapshots[key] = {"http_status": 200, "data": representation}
    with pytest.raises(PlayError, match="positively observed"):
        svc.prepare(method, p, b, True)
    assert not writes


@pytest.mark.parametrize(
    "kind",
    [
        "foreign_app",
        "foreign_parent",
        "inner_wildcard",
        "duplicate",
        "empty",
        "oversize",
        "both_states",
        "no_state",
    ],
)
def test_state_batches_cannot_escape_parent(monkeypatch, kind):
    method = PREFIX + "purchaseOptions.offers.batchUpdateStates"
    svc, p, b, _, _, writes, auth = service(monkeypatch, method)
    action = b["requests"][0]["activateOneTimeProductOfferRequest"]
    if kind == "foreign_app":
        action["packageName"] = "com.other.app"
    if kind == "foreign_parent":
        action["productId"] = "other"
    if kind == "inner_wildcard":
        action["purchaseOptionId"] = "-"
    if kind == "duplicate":
        b["requests"] *= 2
    if kind == "empty":
        b["requests"] = []
    if kind == "oversize":
        b["requests"] *= 101
    if kind == "both_states":
        b["requests"][0]["cancelOneTimeProductOfferRequest"] = action
    if kind == "no_state":
        b["requests"] = [{}]
    with pytest.raises(PlayError):
        svc.prepare(method, p, b, True)
    assert not auth and not writes


def test_cancel_and_force_effects_are_explicit():
    method = PREFIX + "purchaseOptions.offers.cancel"
    p, b = case(method)
    assert "ALL pending orders" in request_spec(method, p, b)["effects"]
    method = PREFIX + "purchaseOptions.batchDelete"
    p, b = case(method)
    b["requests"][0]["force"] = True
    effects = request_spec(method, p, b)["effects"]
    assert "every child offer" in effects and "does not inventory" in effects


def test_purchase_option_deletion_requires_distinct_parent_products():
    method = PREFIX + "purchaseOptions.batchDelete"
    p, b = case(method)
    b["requests"].append({**OPTION, "purchaseOptionId": "another"})
    with pytest.raises(PlayError, match="different parent products"):
        request_spec(method, p, b)


@pytest.mark.parametrize(
    "change",
    [
        "mask",
        "wildcard",
        "identity",
        "unmasked",
        "output",
        "nested_output",
        "missing_create_body",
        "ambiguous_option",
    ],
)
def test_product_body_boundaries(monkeypatch, change):
    method = PREFIX + "patch"
    svc, p, b, _, _, writes, auth = service(monkeypatch, method, True)
    if change == "mask":
        p.pop("updateMask")
    if change == "wildcard":
        p["updateMask"] = "*"
    if change == "identity":
        b["productId"] = "other"
    if change == "unmasked":
        b["offerTags"] = []
    if change == "output":
        b["regionsVersion"] = {"version": "future"}
    if change == "nested_output":
        b["purchaseOptions"][0]["state"] = "ACTIVE"
    if change == "missing_create_body":
        b["purchaseOptions"] = []
    if change == "ambiguous_option":
        b["purchaseOptions"][0]["rentOption"] = {}
    with pytest.raises(PlayError):
        svc.prepare(method, p, b, True)
    assert not auth and not writes


@pytest.mark.parametrize(
    "mode", ["drift", "404", "204", "other_identity", "missing_option", "wrong_response"]
)
def test_existing_offer_changes_require_positive_stable_baseline(monkeypatch, mode):
    method = PREFIX + "purchaseOptions.offers.deactivate"
    svc, p, b, snapshots, _, writes, _ = service(monkeypatch, method)
    op = svc.prepare(method, p, b, True)["operation_id"]
    key = next(
        key
        for key, expected in observations(request_spec(method, p, b)).items()
        if expected["offer"]
    )
    if mode == "drift":
        snapshots[key]["data"]["oneTimeProductOffers"][0]["state"] = "ACTIVE"
    if mode == "404":
        snapshots[key] = {"http_status": 404, "data": None}
    if mode == "204":
        snapshots[key] = {"http_status": 204, "data": None}
    if mode == "other_identity":
        snapshots[key]["data"]["oneTimeProductOffers"][0]["packageName"] = "com.other.app"
    if mode == "missing_option":
        next(v for k, v in snapshots.items() if k != key)["data"]["purchaseOptions"] = []
    if mode == "wrong_response":
        monkeypatch.setattr(monetization_http, "execute", lambda *a: {"packageName": APP})
    with pytest.raises(PlayError):
        svc.apply(op, op)
    assert not writes
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(op, op)


@pytest.mark.parametrize(
    "method",
    [
        PREFIX + "batchDelete",
        PREFIX + "purchaseOptions.batchDelete",
        PREFIX + "purchaseOptions.offers.batchDelete",
    ],
)
@pytest.mark.parametrize("status,content", [(200, b""), (204, b""), (200, b"{}")])
def test_post_deletion_transport_accepts_empty_success(monkeypatch, method, status, content):
    class Response(io.BytesIO):
        pass

    response = Response(content)
    response.status = status
    sent = []

    def open_request(req, timeout):
        sent.append(req)
        return response

    monkeypatch.setattr(
        monetization_http.urllib.request,
        "build_opener",
        lambda *a: Mock(handlers=[], open=open_request),
    )
    p, b = case(method)
    assert monetization_http.execute(method, p, b, "synthetic") == {}
    assert len(sent) == 1 and sent[0].method == "POST"


def test_pinned_case_sensitive_product_paths_and_long_identifiers():
    p, b = case(PREFIX + "patch")
    p["productId"] = b["productId"] = "a" * 100
    spec = request_spec(PREFIX + "patch", p, b)
    assert "/onetimeproducts/" in spec["url"]
    assert "/oneTimeProducts/" in next(iter(observations(spec)))
    method = PREFIX + "purchaseOptions.offers.batchGet"
    p = {"packageName": APP, "productId": "a" * 100, "purchaseOptionId": "buy-"}
    assert request_spec(method, p, {"requests": [{**p, "offerId": "sale-"}]}, query=True)


def test_batch_response_order_and_partial_success_are_not_assumed():
    from pubship.monetization_contracts import validate_mutation_response

    method = PREFIX + "batchUpdate"
    p, b = case(method)
    second = deepcopy(b["requests"][0])
    second["oneTimeProduct"]["productId"] = "second"
    b["requests"].append(second)
    spec = request_spec(method, p, b)
    with pytest.raises(PlayError):
        validate_mutation_response(
            spec, {"oneTimeProducts": [{"packageName": APP, "productId": "second"}, PRODUCT]}
        )
    with pytest.raises(PlayError):
        validate_mutation_response(spec, {"oneTimeProducts": [PRODUCT]})
    validate_mutation_response(
        spec, {"oneTimeProducts": [PRODUCT, {"packageName": APP, "productId": "second"}]}
    )


def test_real_stdio_all_one_time_operations(tmp_path):
    import asyncio
    import sys

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    script = tmp_path / "synthetic_onetime.py"
    script.write_text("""
from pubship import monetization_http
from pubship.auth import Auth
from pubship.config import Settings
from pubship.coverage import ONETIME_WRITE_METHODS
from pubship.monetization_contracts import request_spec
from pubship.server import create_server
APP="com.example.app"
Auth.token=lambda self,scope:"synthetic-only"
def observe(url,token):
    parts=url.split("/")
    return {"http_status":200,"data":{"packageName":APP,"productId":parts[-1],"purchaseOptions":[{"purchaseOptionId":"lifetime","state":"ACTIVE"}]}}
def observe_query(method,parameters,body,token):
    return {"http_status":200,"data":{"oneTimeProductOffers":body["requests"]}}
def execute(method,parameters,body,token):
    spec=request_spec(method,parameters,body)
    if spec["empty_response"]:return {}
    rows=[dict(t) if ".offers." in method else {k:t[k] for k in ("packageName","productId")} for t in spec["targets"]]
    return {"oneTimeProductOffers" if ".offers." in method else "oneTimeProducts":rows} if ".batch" in method else rows[0]
monetization_http.observe=observe
monetization_http.observe_query=observe_query
monetization_http.execute=execute
p=frozenset({APP})
create_server(Settings(packages=p,monetization_packages=p,monetization_methods=ONETIME_WRITE_METHODS)).run()
""")

    async def exercise():
        async with Client(
            StdioServerParameters(command=sys.executable, args=[str(script)])
        ) as client:
            for method in sorted(ONETIME_WRITE_METHODS):
                p, b = case(method)
                preview = await client.call_tool(
                    "prepare_monetization_write",
                    {
                        "method": method,
                        "parameters": p,
                        "body": b,
                        "acknowledge_live_effects": True,
                    },
                )
                assert not preview.is_error, (method, preview.content)
                op = preview.structured_content["operation_id"]
                result = await client.call_tool(
                    "apply_monetization_write", {"operation_id": op, "confirmation": op}
                )
                assert not result.is_error, (method, result.content)
                assert result.structured_content["mutation_succeeded"]
            replay = await client.call_tool(
                "apply_monetization_write", {"operation_id": op, "confirmation": op}
            )
            assert replay.is_error

    asyncio.run(exercise())
