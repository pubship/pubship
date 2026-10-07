"""Protocol and failure evidence for the new support bundle, with synthetic Google IO."""

import asyncio
import io
import json
import urllib.error
from copy import deepcopy
from unittest.mock import Mock

import pytest
from mcp import Client

from pubship import support_http
from pubship.config import Settings
from pubship.coverage import SHARING_METHODS, SUPPORT_METHODS
from pubship.errors import PlayError
from pubship.server import create_server
from pubship.support import SupportOperations
from pubship.support_contracts import request_spec

APP = "com.example.app"
PREFIX = "androidpublisher."
P = {"packageName": APP}
REVIEW = {
    "reviewId": "r1",
    "comments": [
        {"userComment": {"text": "Useful", "starRating": 5, "lastModified": {"seconds": "100"}}}
    ],
}
TARGET = {"regions": {"regionCode": ["CA"]}, "versionList": {"versionCodes": ["42"]}}
ACTION = {
    "appRecoveryId": "7",
    "status": "RECOVERY_STATUS_DRAFT",
    "lastUpdateTime": "2026-10-06T00:00:00Z",
    "targeting": TARGET,
}
DEVICE = {"screenDensity": 480, "supportedAbis": ["arm64-v8a"], "supportedLocales": ["en-US"]}
CONFIG = {"userCountrySets": [{"name": "north", "countryCodes": ["US", "CA"]}]}


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status = status


def case(suffix):
    p, body, mutation, before, after, version = deepcopy(P), {}, {}, None, None, None
    if suffix == "reviews.reply":
        p["reviewId"] = "r1"
        body = {"replyText": "Thanks!"}
        mutation = {"result": {"replyText": "Thanks!", "lastEdited": {"seconds": "200"}}}
        before, after = deepcopy(REVIEW), deepcopy(REVIEW)
        after["comments"].append(
            {"developerComment": {"text": "Thanks!", "lastModified": {"seconds": "200"}}}
        )
    elif suffix == "applications.dataSafety":
        body = {"safetyLabels": "Question ID,Response value\nexample,TRUE\n"}
    elif suffix == "applications.deviceTierConfigs.create":
        body = deepcopy(CONFIG)
        mutation = {**body, "deviceTierConfigId": "8"}
        after = mutation
    elif suffix == "systemapks.variants.create":
        p["versionCode"] = "42"
        body = {"deviceSpec": deepcopy(DEVICE)}
        mutation = {**body, "variantId": 9}
        after = mutation
    elif suffix == "apprecovery.create":
        body = {
            "targeting": deepcopy(TARGET),
            "remoteInAppUpdate": {"isRemoteInAppUpdateRequested": True},
        }
        mutation = deepcopy(ACTION)
        after = {"recoveryActions": [mutation]}
    else:
        p["appRecoveryId"] = "7"
        version = "42"
        action = deepcopy(ACTION)
        if suffix.endswith("cancel"):
            action["status"] = "RECOVERY_STATUS_ACTIVE"
        before = {"recoveryActions": [action]}
        after = deepcopy(before)
        if suffix.endswith("deploy"):
            after["recoveryActions"][0]["status"] = "RECOVERY_STATUS_ACTIVE"
        elif suffix.endswith("cancel"):
            after["recoveryActions"][0]["status"] = "RECOVERY_STATUS_CANCELED"
        else:
            body = {"targetingUpdate": {"regions": {"regionCode": ["US"]}}}
            after["recoveryActions"][0]["targeting"]["regions"]["regionCode"].append("US")
    return p, body, mutation, before, after, version


def configured():
    return Settings(
        packages=frozenset({APP}),
        support_packages=frozenset({APP}),
        support_methods=SUPPORT_METHODS,
    )


def mock_responses(monkeypatch, responses):
    queue = list(responses)
    calls = []

    def send(req, timeout):
        calls.append(req)
        assert timeout == 15
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(
        "urllib.request.build_opener", lambda *handlers: Mock(handlers=[], open=send)
    )
    return calls, queue


@pytest.mark.parametrize("method", sorted(SUPPORT_METHODS))
def test_each_operation_full_prepare_http_apply_and_readback(monkeypatch, method):
    suffix = method.removeprefix(PREFIX)
    p, body, mutation, before, after, version = case(suffix)
    responses = [Response(before), Response(before)] if before is not None else []
    responses += [Response(mutation)]
    if after is not None:
        responses.append(Response(after))
    calls, queue = mock_responses(monkeypatch, responses)
    auth = Mock()
    auth.token.return_value = "synthetic-original"
    svc = SupportOperations(configured(), auth, local=True)
    proposed = svc.prepare(method, p, body, True, version)
    assert all(r.get_method() == "GET" for r in calls)
    auth.token.return_value = "synthetic-replacement"
    result = svc.apply(proposed["operation_id"], proposed["operation_id"])
    assert result["mutation_succeeded"] and not result["availability_verified"]
    assert result["stale_check_performed"] == (before is not None)
    assert result["readback_succeeded"] is (True if after is not None else None)
    writes = [r for r in calls if r.get_method() == "POST"]
    assert len(writes) == 1 and json.loads(writes[0].data) == body
    assert "observation_version_code" not in writes[0].full_url
    assert all(r.get_header("Authorization") == "Bearer synthetic-original" for r in calls)
    assert auth.token.call_count == 1 and not queue
    assert "synthetic-original" not in json.dumps(result)


@pytest.mark.parametrize("status,payload", [(200, b""), (200, b"{}"), (204, b"")])
def test_declared_empty_successes_are_accepted(monkeypatch, status, payload):
    mock_responses(monkeypatch, [Response(payload, status)])
    assert (
        support_http.execute(PREFIX + "applications.dataSafety", P, {"safetyLabels": "csv"}, "fake")
        == {}
    )


@pytest.mark.parametrize(
    "payload", [b"null", b"[]", b"not-json", b'{"bad":NaN}', b"x" * (support_http.MAX_BYTES + 1)]
)
def test_malformed_write_response_is_unknown_never_retried(monkeypatch, payload):
    calls, queue = mock_responses(monkeypatch, [Response(payload)])
    with pytest.raises(PlayError, match="outcome unknown"):
        support_http.execute(
            PREFIX + "reviews.reply", {**P, "reviewId": "r1"}, {"replyText": "Thanks!"}, "fake"
        )
    assert len(calls) == 1 and not queue


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("private response"),
        urllib.error.HTTPError("https://private.example/secret", 503, "private response", {}, None),
        PlayError("Unexpected redirect blocked"),
    ],
)
def test_write_transport_failure_redacts_and_never_retries(monkeypatch, error):
    calls, _ = mock_responses(monkeypatch, [error])
    with pytest.raises(PlayError) as caught:
        support_http.execute(PREFIX + "applications.dataSafety", P, {"safetyLabels": "csv"}, "fake")
    assert "outcome unknown" in str(caught.value) and "private" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x",
        "http://androidpublisher.googleapis.com/x",
        "https://androidpublisher.googleapis.com@evil.example/x",
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/a#fragment",
    ],
)
def test_transport_fixed_host_guard(url):
    with pytest.raises(PlayError, match="fixed Google"):
        support_http._request(url, "fake", body={})


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "continuation",
        "bad-time",
        "wrong-version",
        "empty-regions",
        "false-all-users",
        "empty-sdk",
    ],
)
def test_recovery_observation_fails_closed(monkeypatch, change):
    spec = request_spec(PREFIX + "apprecovery.deploy", {**P, "appRecoveryId": "7"}, {})
    action = deepcopy(ACTION)
    response = {"recoveryActions": [action]}
    if change == "missing":
        response = {}
    elif change == "duplicate":
        response["recoveryActions"].append(deepcopy(action))
    elif change == "continuation":
        response["nextPageToken"] = "unknown"
    elif change == "bad-time":
        action["lastUpdateTime"] = "not-a-timestamp"
    elif change == "wrong-version":
        action["targeting"]["versionList"]["versionCodes"] = ["43"]
    elif change == "empty-regions":
        action["targeting"]["regions"] = {}
    elif change == "false-all-users":
        action["targeting"] = {
            "versionList": {"versionCodes": ["42"]},
            "allUsers": {"isAllUsersRequested": False},
        }
    elif change == "empty-sdk":
        action["targeting"] = {"versionList": {"versionCodes": ["42"]}, "androidSdks": {}}
    mock_responses(monkeypatch, [Response(response)])
    with pytest.raises(PlayError):
        support_http.observe(spec, "fake", "42")


def test_volatile_counts_do_not_invalidate_intent():
    spec = request_spec(PREFIX + "reviews.reply", {**P, "reviewId": "r1"}, {"replyText": "Thanks!"})
    review = deepcopy(REVIEW)
    review["comments"][0]["userComment"]["thumbsUpCount"] = 99
    assert support_http.fingerprint(spec, REVIEW) == support_http.fingerprint(spec, review)
    spec = request_spec(PREFIX + "apprecovery.deploy", {**P, "appRecoveryId": "7"}, {})
    changed = deepcopy(ACTION)
    changed["remoteInAppUpdateData"] = {
        "remoteAppUpdateDataPerBundle": [{"versionCode": "42", "recoveredDeviceCount": "4"}]
    }
    assert support_http.fingerprint(spec, ACTION) == support_http.fingerprint(spec, changed)
    changed["targeting"]["regions"]["regionCode"].append("US")
    assert support_http.fingerprint(spec, ACTION) != support_http.fingerprint(spec, changed)


def test_actual_mcp_support_call_and_sharing_registration(monkeypatch):
    responses = [
        Response(REVIEW),
        Response(REVIEW),
        Response({"result": {"replyText": "Thanks!", "lastEdited": {"seconds": "200"}}}),
        Response(REVIEW),
    ]
    calls, _ = mock_responses(monkeypatch, responses)
    monkeypatch.setattr("pubship.auth.Auth.token", lambda *a: "fake")

    async def check():
        async with Client(create_server(configured())) as client:
            tools = (await client.list_tools()).tools
            names = {t.name for t in tools}
            assert {"prepare_support_operation", "apply_support_operation"} <= names
            result = await client.call_tool(
                "prepare_support_operation",
                {
                    "method": PREFIX + "reviews.reply",
                    "parameters": {**P, "reviewId": "r1"},
                    "body": {"replyText": "Thanks!"},
                    "acknowledge_live_effects": True,
                },
            )
            assert not result.is_error
            op = result.structured_content["operation_id"]
            result = await client.call_tool(
                "apply_support_operation", {"operation_id": op, "confirmation": op}
            )
            assert not result.is_error and result.structured_content["mutation_succeeded"]
            result = await client.call_tool(
                "prepare_support_operation",
                {
                    "method": PREFIX + "reviews.reply",
                    "parameters": {**P, "reviewId": "r1"},
                    "body": {"replyText": "Thanks!"},
                    "acknowledge_live_effects": "true",
                },
            )
            assert result.is_error
        settings = Settings(
            packages=frozenset({APP}),
            artifact_packages=frozenset({APP}),
            artifact_methods=SHARING_METHODS,
        )
        async with Client(create_server(settings)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"prepare_internal_sharing_upload", "apply_internal_sharing_upload"} <= names
            assert (
                not {
                    "prepare_artifact_upload",
                    "apply_artifact_upload",
                    "prepare_support_operation",
                }
                & names
            )

    asyncio.run(check())
    assert sum(r.get_method() == "POST" for r in calls) == 1
