"""Synthetic account contracts, isolation, pagination and single-attempt lifecycle evidence."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest
from mcp import Client
from mcp.server import MCPServer

from pubship import account_access_http as http
from pubship.account_access import AccountAccess
from pubship.account_access_config import METHODS, READ_METHOD, AccountAccessSettings
from pubship.account_access_contracts import request_spec
from pubship.account_access_tools import build_account_tools
from pubship.errors import PlayError
from pubship.server import safe_tool

D = "123456789"
U = "person@example.test"
APP = "com.example.app"
NAME = f"developers/{D}/users/{U}"
GRANT = NAME + "/grants/" + APP
PERM = "CAN_VIEW_NON_FINANCIAL_DATA_GLOBAL"
APP_PERM = "CAN_VIEW_NON_FINANCIAL_DATA"
USER = {
    "name": NAME,
    "email": U,
    "developerAccountPermissions": [PERM],
    "partial": False,
    "grants": [{"name": GRANT, "packageName": APP, "appLevelPermissions": [APP_PERM]}],
}
METHOD = "androidpublisher.users.patch"
PARAMS = {"name": NAME, "updateMask": "developerAccountPermissions"}
BODY = {"developerAccountPermissions": [PERM]}


def config(**overrides):
    args = dict(
        developers=frozenset({D}),
        methods=METHODS,
        users=frozenset({U}),
        apps=frozenset({APP}),
        developer_permissions=frozenset({PERM}),
        app_permissions=frozenset({APP_PERM}),
    )
    args.update(overrides)
    return AccountAccessSettings(**args)


@pytest.fixture(autouse=True)
def no_live_requests():
    with patch(
        "urllib.request.OpenerDirector.open", side_effect=AssertionError("No live requests")
    ):
        yield


@pytest.fixture
def service(monkeypatch):
    auth = Mock()
    auth.token.return_value = "synthetic-private-original-token"
    svc = AccountAccess(config(), auth, local=True)
    observe = Mock(side_effect=lambda *a: deepcopy(USER))
    request = Mock(return_value=deepcopy(USER))
    monkeypatch.setattr(http, "observe", observe)
    monkeypatch.setattr(http, "request", request)
    return svc, auth, observe, request


def prepare(svc):
    return svc.prepare(METHOD, PARAMS, BODY, True)


def apply(svc, p):
    return svc.apply(p["operation_id"], p["operation_id"])


def test_all_seven_routes_and_patch_reset():
    cases = [
        (READ_METHOD, {"parent": f"developers/{D}"}, None, "GET"),
        (
            "androidpublisher.users.create",
            {"parent": f"developers/{D}"},
            {"name": NAME, "email": U},
            "POST",
        ),
        (METHOD, PARAMS, BODY, "PATCH"),
        ("androidpublisher.users.delete", {"name": NAME}, None, "DELETE"),
        (
            "androidpublisher.grants.create",
            {"parent": NAME},
            {"name": GRANT, "packageName": APP},
            "POST",
        ),
        (
            "androidpublisher.grants.patch",
            {"name": GRANT, "updateMask": "appLevelPermissions"},
            {},
            "PATCH",
        ),
        ("androidpublisher.grants.delete", {"name": GRANT}, None, "DELETE"),
    ]
    for method, params, body, verb in cases:
        spec = request_spec(method, params, body)
        assert spec["http_method"] == verb
        assert spec["developer"] == D
        assert spec["url"].startswith(
            "https://androidpublisher.googleapis.com/androidpublisher/v3/developers/"
        )
    assert "%40" in request_spec(METHOD, PARAMS, {})["url"]


@pytest.mark.parametrize(
    "params,body",
    [
        ({"name": NAME, "updateMask": "*"}, BODY),
        ({"name": NAME, "updateMask": "email"}, {"email": U}),
        ({"name": NAME}, BODY),
        (PARAMS, {"partial": False}),
        (PARAMS, {"email": "other@example.test"}),
        (PARAMS, {"name": NAME.replace(D, "999")}),
        (PARAMS, {"developerAccountPermissions": [PERM, PERM]}),
        (PARAMS, {"developerAccountPermissions": ["DEVELOPER_LEVEL_PERMISSION_UNSPECIFIED"]}),
        (PARAMS, {"expirationTime": "2000-01-01T00:00:00Z"}),
    ],
)
def test_invalid_write_contract(params, body):
    with pytest.raises(PlayError):
        request_spec(METHOD, params, body)


@pytest.mark.parametrize(
    "name",
    [
        NAME + "/extra",
        NAME.replace(U, "../evil"),
        NAME.replace(U, "a%2fb@example.test"),
        NAME.replace(D, "*"),
        NAME + "\n",
    ],
)
def test_path_injection_rejected(name):
    with pytest.raises(PlayError):
        request_spec(METHOD, {**PARAMS, "name": name}, BODY)


@pytest.mark.parametrize(
    "overrides",
    [
        {"developers": frozenset()},
        {"users": frozenset()},
        {"methods": frozenset({METHOD})},
        {"developer_permissions": frozenset()},
    ],
)
def test_missing_authority_before_auth(service, overrides):
    svc, auth, observe, request = service
    svc.settings = config(**overrides)
    with pytest.raises(PlayError):
        prepare(svc)
    auth.token.assert_not_called()
    observe.assert_not_called()
    request.assert_not_called()


@pytest.mark.parametrize("ack", [False, 1, "true", None])
def test_literal_ack_required(service, ack):
    svc, auth, observe, request = service
    with pytest.raises(PlayError):
        svc.prepare(METHOD, PARAMS, BODY, ack)
    auth.token.assert_not_called()


def test_original_token_frozen_body_single_use(service):
    svc, auth, observe, request = service
    p = prepare(svc)
    p["proposed_request"]["body"]["developerAccountPermissions"] = []
    auth.token.return_value = "synthetic-new-token"
    result = apply(svc, p)
    assert result["mutation_succeeded"] and not result["permission_propagation_verified"]
    assert request.call_args.args[1] == "synthetic-private-original-token"
    assert request.call_args.args[0]["body"] == BODY
    assert auth.token.call_count == 1
    with pytest.raises(PlayError):
        apply(svc, p)
    assert request.call_count == 1
    assert "synthetic-private" not in json.dumps(result)


@pytest.mark.parametrize("change", ["partial", "permissions", "absence", "revoked"])
def test_changed_baseline_or_revoked_scope_aborts(service, change):
    svc, auth, observe, request = service
    p = prepare(svc)
    if change == "partial":
        observe.side_effect = lambda *a: {**USER, "partial": True}
    elif change == "permissions":
        observe.side_effect = lambda *a: {**USER, "developerAccountPermissions": []}
    elif change == "absence":
        observe.side_effect = lambda *a: None
    else:
        svc.settings = replace(svc.settings, users=frozenset())
    with pytest.raises(PlayError):
        apply(svc, p)
    request.assert_not_called()


def test_partial_prepare_and_user_delete_outside_app_scope(service):
    svc, auth, observe, request = service
    observe.side_effect = lambda *a: {**USER, "partial": True}
    with pytest.raises(PlayError, match="Partial"):
        prepare(svc)
    observe.side_effect = lambda *a: deepcopy(USER)
    svc.settings = replace(svc.settings, apps=frozenset())
    with pytest.raises(PlayError, match="app"):
        svc.prepare("androidpublisher.users.delete", {"name": NAME}, None, True)
    request.assert_not_called()


def test_readback_failure_does_not_hide_success(service):
    svc, auth, observe, request = service
    p = prepare(svc)
    observe.side_effect = [deepcopy(USER), PlayError("private backend details")]
    result = apply(svc, p)
    assert result["mutation_succeeded"] and result["readback_succeeded"] is False
    assert "private backend" not in json.dumps(result)


def test_failure_consumes_operation_without_retry(service):
    svc, auth, observe, request = service
    p = prepare(svc)
    request.side_effect = PlayError(http.UNKNOWN_OUTCOME)
    with pytest.raises(PlayError, match="unknown"):
        apply(svc, p)
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, p)
    assert request.call_count == 1


def test_confirmation_mismatch_does_not_consume(service):
    svc, auth, observe, request = service
    p = prepare(svc)
    with pytest.raises(PlayError):
        svc.apply(p["operation_id"], "wrong")
    assert apply(svc, p)["mutation_succeeded"]


def test_local_only_and_expiry(service, monkeypatch):
    svc, auth, observe, request = service
    svc.local = False
    with pytest.raises(PlayError):
        prepare(svc)
    auth.token.assert_not_called()
    svc.local = True
    p = prepare(svc)
    monkeypatch.setattr("pubship.account_access.time.monotonic", lambda: 10**15)
    with pytest.raises(PlayError, match="expired"):
        apply(svc, p)
    request.assert_not_called()


def test_pagination_complete_and_private_target_only(monkeypatch):
    other = {"name": f"developers/{D}/users/other@example.test", "email": "other@example.test"}
    req = Mock(
        side_effect=[{"users": [other], "nextPageToken": "next"}, {"users": [deepcopy(USER)]}]
    )
    monkeypatch.setattr(http, "request", req)
    observed = http.observe(request_spec(METHOD, PARAMS, BODY), "synthetic", 10**15)
    assert observed == USER
    assert req.call_count == 2
    assert req.call_args.args[0]["parameters"]["pageToken"] == "next"


@pytest.mark.parametrize(
    "pages",
    [
        [{"users": [USER], "nextPageToken": "repeat"}, {"nextPageToken": "repeat"}],
        [{"users": [USER], "nextPageToken": "next"}, {"users": [USER]}],
        [{"users": [{**USER, "name": NAME.replace(D, "999")}]}],
    ],
)
def test_incomplete_or_mismatched_pagination_fails(monkeypatch, pages):
    monkeypatch.setattr(http, "request", Mock(side_effect=deepcopy(pages)))
    with pytest.raises(PlayError):
        http.observe(request_spec(METHOD, PARAMS, BODY), "synthetic", 10**15)


def test_pagination_bound(monkeypatch):
    monkeypatch.setattr(http, "MAX_PAGES", 1)
    monkeypatch.setattr(http, "request", Mock(return_value={"nextPageToken": "next"}))
    with pytest.raises(PlayError, match="page limit"):
        http.observe(request_spec(METHOD, PARAMS, BODY), "synthetic", 10**15)


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.body[:size]


def test_transport_empty_delete_and_redaction(monkeypatch):
    opener = Mock(handlers=[])
    monkeypatch.setattr("urllib.request.build_opener", Mock(return_value=opener))
    spec = request_spec("androidpublisher.users.delete", {"name": NAME})
    opener.open.return_value = Response(204, b"")
    assert http.request(spec, "synthetic") == {}
    assert opener.open.call_args.args[0].method == "DELETE"
    opener.open.return_value = Response(200, b"private@example.test not-json")
    with pytest.raises(PlayError) as error:
        http.request(spec, "synthetic")
    assert "private@example.test" not in str(error.value)
    assert "unknown" in str(error.value)


def test_env_validation_and_disabled_tools(monkeypatch):
    assert build_account_tools(AccountAccessSettings(), Mock(), safe_tool) == []
    monkeypatch.setenv("GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS", "*")
    with pytest.raises(PlayError):
        AccountAccessSettings.from_env()
    monkeypatch.setenv("GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS", D)
    monkeypatch.setenv("GOOGLE_PLAY_ACCOUNT_ACCESS_METHODS", READ_METHOD)
    settings = AccountAccessSettings.from_env()
    assert settings.developers == {D}
    assert [t.name for t in build_account_tools(settings, Mock(), safe_tool)] == [
        "read_account_access"
    ]


def test_mcp_contracts_and_read_boundary(monkeypatch):
    auth = Mock()
    auth.token.return_value = "synthetic"
    monkeypatch.setattr(http, "request", Mock(return_value={"users": [deepcopy(USER)]}))
    server = MCPServer("account-test", tools=build_account_tools(config(), auth, safe_tool))

    async def run():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {
                "read_account_access",
                "prepare_account_access",
                "apply_account_access",
            }
            for tool in tools:
                assert tool.input_schema["additionalProperties"] is False
            result = await client.call_tool(
                "read_account_access",
                {"method": READ_METHOD, "parameters": {"parent": f"developers/{D}"}},
            )
            assert not result.is_error
            denied = await client.call_tool(
                "read_account_access",
                {"method": READ_METHOD, "parameters": {"parent": "developers/999"}},
            )
            assert denied.is_error

    asyncio.run(run())


@pytest.mark.parametrize(
    "method,params,body,before,mutation",
    [
        (
            "androidpublisher.users.create",
            {"parent": f"developers/{D}"},
            {"name": NAME, "email": U, "developerAccountPermissions": [PERM]},
            None,
            {**USER, "accessState": "INVITED"},
        ),
        ("androidpublisher.users.delete", {"name": NAME}, None, USER, {}),
        (
            "androidpublisher.grants.create",
            {"parent": NAME},
            USER["grants"][0],
            {**USER, "grants": []},
            USER["grants"][0],
        ),
        (
            "androidpublisher.grants.patch",
            {"name": GRANT, "updateMask": "appLevelPermissions"},
            {"appLevelPermissions": []},
            USER,
            {**USER["grants"][0], "appLevelPermissions": []},
        ),
        ("androidpublisher.grants.delete", {"name": GRANT}, None, USER, {}),
    ],
)
def test_other_mutation_lifecycles(service, method, params, body, before, mutation):
    svc, auth, observe, request = service
    observe.side_effect = lambda *a: deepcopy(before)
    request.return_value = deepcopy(mutation)
    p = svc.prepare(method, params, body, True)
    result = apply(svc, p)
    assert result["mutation_succeeded"] and result["mutation_response"] == mutation
    assert request.call_count == 1


@pytest.mark.parametrize("stamp", ["2099-01-01T00:00:00Z", "2099-01-01T00:00:00.123456789+01:00"])
def test_future_expiration_accepted(stamp):
    result = request_spec(
        METHOD, {"name": NAME, "updateMask": "expirationTime"}, {"expirationTime": stamp}
    )
    assert result["body"]["expirationTime"] == stamp


@pytest.mark.parametrize(
    "stamp",
    ["2099-01-01", "2099-01-01T00:00:00", "2099-02-30T00:00:00Z", "2099-01-01T00:00:00+99:00"],
)
def test_invalid_expiration_rejected(stamp):
    with pytest.raises(PlayError):
        request_spec(
            METHOD, {"name": NAME, "updateMask": "expirationTime"}, {"expirationTime": stamp}
        )


def test_collision_safe_ids_and_expired_purge(service, monkeypatch):
    svc, auth, observe, request = service
    first = prepare(svc)
    original = first["operation_id"].removeprefix("account_")
    monkeypatch.setattr(
        "pubship.account_access.secrets.token_urlsafe",
        Mock(side_effect=[original, "b" * 43]),
    )
    second = prepare(svc)
    assert second["operation_id"] != first["operation_id"]
    assert len(svc._preparations) == 2
    svc._preparations[first["operation_id"]].deadline = 0
    with pytest.raises(PlayError):
        svc.apply("account_" + "z" * 43, "account_" + "z" * 43)
    assert first["operation_id"] not in svc._preparations


def test_concurrent_apply_only_sends_once(service):
    from concurrent.futures import ThreadPoolExecutor

    svc, auth, observe, request = service
    p = prepare(svc)

    def attempt():
        try:
            return apply(svc, p)["mutation_succeeded"]
        except PlayError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    assert request.call_count == 1


def test_wrong_mutation_identity_is_unknown_consumed(service):
    svc, auth, observe, request = service
    p = prepare(svc)
    request.return_value = {**USER, "name": NAME.replace(D, "999")}
    with pytest.raises(PlayError, match="unknown"):
        apply(svc, p)
    assert p["operation_id"] not in svc._preparations


def test_sparse_protojson_defaults_and_explicit_partial(service):
    svc, auth, observe, request = service
    sparse = {"name": NAME, "email": U}
    observe.side_effect = lambda *a: sparse
    p = svc.prepare(METHOD, PARAMS, {"developerAccountPermissions": []}, True)
    assert p["before"] == sparse
    observe.side_effect = lambda *a: {**sparse, "partial": True}
    with pytest.raises(PlayError, match="Partial"):
        apply(svc, p)


def test_http_no_redirect_retry_and_body_leak(monkeypatch):
    import urllib.error

    from pubship.http import NoRedirect

    opener, factory = Mock(handlers=[]), Mock()
    factory.return_value = opener
    monkeypatch.setattr("urllib.request.build_opener", factory)
    error = urllib.error.HTTPError("https://private@example.test", 403, "private secret", {}, None)
    opener.open.side_effect = error
    with pytest.raises(PlayError) as caught:
        http.request(request_spec(METHOD, PARAMS, BODY), "synthetic")
    assert "private" not in str(caught.value)
    assert "403" in str(caught.value)
    assert opener.open.call_count == 1
    assert isinstance(factory.call_args.args[0], NoRedirect)


def test_app_permission_ceiling_and_delete_coverage(service):
    svc, auth, observe, request = service
    svc.settings = replace(svc.settings, app_permissions=frozenset())
    with pytest.raises(PlayError, match="ceiling"):
        svc.prepare("androidpublisher.grants.delete", {"name": GRANT}, None, True)
    with pytest.raises(PlayError, match="ceiling"):
        svc.prepare("androidpublisher.users.delete", {"name": NAME}, None, True)
    request.assert_not_called()


def test_draft_app_grant_exact_id():
    name = NAME + "/grants/1234567"
    spec = request_spec(
        "androidpublisher.grants.create",
        {"parent": NAME},
        {"name": name, "packageName": "", "appLevelPermissions": [APP_PERM]},
    )
    assert spec["app"] == "1234567"
    http.validate_response(spec, {"name": name, "appLevelPermissions": [APP_PERM]})
