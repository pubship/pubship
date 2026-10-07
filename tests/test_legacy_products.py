"""Legacy compatibility permissions and exact managed-product identity checks."""

import asyncio
import io
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from pubship import monetization_http
from pubship.config import Settings
from pubship.coverage import LEGACY_PRODUCT_WRITE_METHODS
from pubship.errors import PlayError
from pubship.legacy_product_contracts import PREFIX
from pubship.monetization import Monetization
from pubship.monetization_contracts import observations, request_spec

APP = "com.example.app"
ID = {"packageName": APP, "sku": "premium"}
RESOURCE = {
    **ID,
    "purchaseType": "managedUser",
    "status": "active",
    "defaultLanguage": "en-US",
    "defaultPrice": {"currency": "USD", "priceMicros": "4990000"},
    "listings": {"en-US": {"title": "Premium", "description": "Thank you"}},
}


def case(method, upsert=False):
    action = method.removeprefix(PREFIX)
    if action == "batchDelete":
        return {"packageName": APP}, {"requests": [dict(ID)]}
    if action == "batchUpdate":
        return {"packageName": APP}, {
            "requests": [{**ID, "inappproduct": deepcopy(RESOURCE), "allowMissing": upsert}]
        }
    if action == "insert":
        return {"packageName": APP}, deepcopy(RESOURCE)
    if action == "delete":
        return dict(ID), None
    if action == "patch":
        return dict(ID), {**ID, "status": "inactive"}
    return {**ID, "allowMissing": upsert}, deepcopy(RESOURCE)


def service(monkeypatch, method, upsert=False):
    reads, writes, auth = [], [], []
    p, b = case(method, upsert)
    spec = request_spec(method, p, b)
    data = {
        key: (
            {"http_status": 404, "data": None}
            if expected["mode"] == "create"
            else {"http_status": 200, "data": deepcopy(RESOURCE)}
        )
        for key, expected in observations(spec).items()
    }
    settings = Settings(
        packages=frozenset({APP}),
        monetization_packages=frozenset({APP}),
        monetization_methods=LEGACY_PRODUCT_WRITE_METHODS,
    )
    svc = Monetization(
        settings,
        SimpleNamespace(token=lambda scope: auth.append(scope) or "synthetic-original"),
        local=True,
    )

    def observe(url, token):
        reads.append((url, token))
        return deepcopy(data[url])

    def execute(m, p, b, token, **kwargs):
        writes.append((m, deepcopy(p), deepcopy(b), token))
        if spec["empty_response"]:
            return {}
        return (
            {"inappproducts": [deepcopy(RESOURCE)]}
            if m.endswith("batchUpdate")
            else deepcopy(RESOURCE)
        )

    monkeypatch.setattr(monetization_http, "observe", observe)
    monkeypatch.setattr(monetization_http, "execute", execute)
    return svc, p, b, data, reads, writes, auth


@pytest.mark.parametrize("method", sorted(LEGACY_PRODUCT_WRITE_METHODS))
def test_every_legacy_write_single_attempt_original_credential_and_replay(monkeypatch, method):
    svc, p, b, _, reads, writes, auth = service(monkeypatch, method)
    preview = svc.prepare(method, p, b, True)
    assert not writes and "managed-product" in preview["effects"]
    op = preview["operation_id"]
    result = svc.apply(op, op)
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert len(writes) == 1 and auth == ["publisher"]
    assert all(token == "synthetic-original" for _, token in reads)
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(op, op)


@pytest.mark.parametrize("method", [PREFIX + "update", PREFIX + "batchUpdate"])
def test_upsert_requires_insert_grant_before_credentials(monkeypatch, method):
    svc, p, b, _, _, writes, auth = service(monkeypatch, method, True)
    svc.settings = Settings(
        packages=frozenset({APP}),
        monetization_packages=frozenset({APP}),
        monetization_methods=frozenset({method}),
    )
    with pytest.raises(PlayError, match="opt-ins"):
        svc.prepare(method, p, b, True)
    assert not auth and not writes


@pytest.mark.parametrize("method", [PREFIX + "update", PREFIX + "batchUpdate"])
def test_explicit_upsert_freezes_unavailable_observation(monkeypatch, method):
    svc, p, b, data, _, writes, _ = service(monkeypatch, method, True)
    data[next(iter(data))] = {"http_status": 404, "data": None}
    preview = svc.prepare(method, p, b, True)
    assert "additional insert" in preview["effects"]
    op = preview["operation_id"]
    assert svc.apply(op, op)["mutation_succeeded"]
    assert len(writes) == 1


@pytest.mark.parametrize(
    "change",
    [
        "body_app",
        "body_sku",
        "nested_app",
        "duplicate",
        "missing_resource",
        "subscription",
        "subscription_field",
        "zero_price",
        "negative_price",
        "bad_currency",
        "missing_listing",
        "missing_type",
    ],
)
def test_legacy_request_boundaries_before_auth(monkeypatch, change):
    method = PREFIX + "batchUpdate"
    svc, p, b, _, _, writes, auth = service(monkeypatch, method)
    item = b["requests"][0]
    resource = item["inappproduct"]
    if change == "body_app":
        resource["packageName"] = "com.other.app"
    if change == "body_sku":
        resource["sku"] = "other"
    if change == "nested_app":
        item["packageName"] = "com.other.app"
    if change == "duplicate":
        b["requests"] *= 2
    if change == "missing_resource":
        item.pop("inappproduct")
    if change == "subscription":
        resource["purchaseType"] = "subscription"
    if change == "subscription_field":
        resource["trialPeriod"] = "P7D"
    if change == "zero_price":
        resource["defaultPrice"]["priceMicros"] = "0"
    if change == "negative_price":
        resource["defaultPrice"]["priceMicros"] = "-1"
    if change == "bad_currency":
        resource["defaultPrice"]["currency"] = "USD\n"
    if change == "missing_listing":
        resource["listings"] = {}
    if change == "missing_type":
        resource.pop("purchaseType")
    with pytest.raises(PlayError):
        svc.prepare(method, p, b, True)
    assert not auth and not writes


@pytest.mark.parametrize(
    "change",
    [
        "drift",
        "wrong_type",
        "wrong_id",
        "404",
        "204",
        "partial_response",
        "wrong_response",
        "revoked_insert",
    ],
)
def test_legacy_preflight_and_uncertain_outcomes(monkeypatch, change):
    method = PREFIX + "batchUpdate"
    svc, p, b, data, _, writes, _ = service(monkeypatch, method, change == "revoked_insert")
    op = svc.prepare(method, p, b, True)["operation_id"]
    key = next(iter(data))
    if change == "drift":
        data[key]["data"]["status"] = "inactive"
    if change == "wrong_type":
        data[key]["data"]["purchaseType"] = "subscription"
    if change == "wrong_id":
        data[key]["data"]["sku"] = "other"
    if change == "404":
        data[key] = {"http_status": 404, "data": None}
    if change == "204":
        data[key] = {"http_status": 204, "data": None}
    if change == "partial_response":
        monkeypatch.setattr(monetization_http, "execute", lambda *a: {"inappproducts": []})
    if change == "wrong_response":
        monkeypatch.setattr(
            monetization_http,
            "execute",
            lambda *a: {"inappproducts": [{**RESOURCE, "purchaseType": "subscription"}]},
        )
    if change == "revoked_insert":
        svc.settings = Settings(
            packages=frozenset({APP}),
            monetization_packages=frozenset({APP}),
            monetization_methods=frozenset({method}),
        )
    with pytest.raises(PlayError):
        svc.apply(op, op)
    assert not writes
    with pytest.raises(PlayError, match="consumed"):
        svc.apply(op, op)


def test_autoconversion_and_complete_put_are_explicit_effects():
    p, b = case(PREFIX + "update")
    p["autoConvertMissingPrices"] = True
    effects = request_spec(PREFIX + "update", p, b)["effects"]
    assert "without merging" in effects and "generate prices" in effects


def test_post_batch_deletion_transport_contract(monkeypatch):
    class Response(io.BytesIO):
        pass

    response = Response(b"")
    response.status = 200
    requests = []

    def open_request(req, timeout):
        requests.append(req)
        return response

    monkeypatch.setattr(
        monetization_http.urllib.request,
        "build_opener",
        lambda *a: Mock(handlers=[], open=open_request),
    )
    p, b = case(PREFIX + "batchDelete")
    assert monetization_http.execute(PREFIX + "batchDelete", p, b, "synthetic") == {}
    assert len(requests) == 1 and requests[0].method == "POST"


def test_real_stdio_legacy_methods(tmp_path):
    script = tmp_path / "legacy_stdio.py"
    script.write_text("""
from pubship import monetization_http
from pubship.auth import Auth
from pubship.config import Settings
from pubship.coverage import LEGACY_PRODUCT_WRITE_METHODS
from pubship.monetization_contracts import request_spec
from pubship.server import create_server
APP="com.example.app"
Auth.token=lambda self,scope:"synthetic"
def observe(url,token):
    return {"http_status":404,"data":None} if url.endswith("/newproduct") else {"http_status":200,"data":{"packageName":APP,"sku":"premium","purchaseType":"managedUser"}}
def execute(method,p,b,token):
    spec=request_spec(method,p,b)
    if spec["empty_response"]:return {}
    rows=[{**t,"purchaseType":"managedUser"} for t in spec["targets"]]
    return {"inappproducts":rows} if method.endswith("batchUpdate") else rows[0]
monetization_http.observe=observe
monetization_http.execute=execute
p=frozenset({APP})
create_server(Settings(packages=p,monetization_packages=p,monetization_methods=LEGACY_PRODUCT_WRITE_METHODS)).run()
""")

    async def exercise():
        async with Client(
            StdioServerParameters(command=sys.executable, args=[str(script)])
        ) as client:
            for method in sorted(LEGACY_PRODUCT_WRITE_METHODS):
                p, b = case(method)
                if method.endswith(".insert"):
                    b["sku"] = "newproduct"
                prepared = await client.call_tool(
                    "prepare_monetization_write",
                    {
                        "method": method,
                        "parameters": p,
                        "body": b,
                        "acknowledge_live_effects": True,
                    },
                )
                assert not prepared.is_error, (method, prepared.content)
                op = prepared.structured_content["operation_id"]
                applied = await client.call_tool(
                    "apply_monetization_write", {"operation_id": op, "confirmation": op}
                )
                assert not applied.is_error, (method, applied.content)
                assert applied.structured_content["mutation_succeeded"]

    asyncio.run(exercise())


@pytest.mark.parametrize("method", sorted(LEGACY_PRODUCT_WRITE_METHODS))
def test_provider_migration_rejection_never_falls_back_or_mutates(monkeypatch, method):
    svc, p, b, _, reads, writes, auth = service(monkeypatch, method)
    attempts = []

    def migration_rejected(url, token):
        attempts.append(url)
        raise PlayError("Google returned HTTP 403: Please migrate to the new publishing API.")

    monkeypatch.setattr(monetization_http, "observe", migration_rejected)
    with pytest.raises(PlayError, match="migrate"):
        svc.prepare(method, p, b, True)
    assert len(attempts) == 1
    assert "/inappproducts/" in attempts[0]
    assert not writes and not reads
    assert auth == ["publisher"]
