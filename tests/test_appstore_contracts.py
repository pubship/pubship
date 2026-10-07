"""Synthetic contracts for all eight fixed app-store methods."""

from copy import deepcopy

import pytest

from pubship.appstore_contracts import (
    APK,
    APPSTORE_METHODS,
    CREATE,
    GET,
    IMAGE,
    LIST,
    POLICY,
    STATUS,
    UPDATE,
    request_spec,
    validate_response,
)
from pubship.errors import PlayError

STORE = "com.example.store"
APP = "com.example.app"
P = {"appStorePackageName": STORE}
TARGET = {**P, "packageName": APP}
UPDATE_BODY = {
    "packageName": APP,
    "appDetails": {"developerName": "Example", "contactEmail": "dev@example.test"},
    "activeLocalizedStoreListings": [
        {
            "languageCode": "en-US",
            "appName": "Example",
            "fullDescription": "Example app",
            "appIconId": "icon-1",
            "screenshotId": ["shot-1"],
        }
    ],
    "activeApks": {"activeApkSets": [{"baseApkId": "apk-1"}]},
    "policyDeclarations": [
        {
            "declarationId": "policy-1",
            "responses": [{"questionId": "q-1", "booleanResponse": {"value": False}}],
        }
    ],
}
RANGE = {**P, "startTime": "2026-10-01T00:00:00Z", "endTime": "2026-10-02T00:00:00Z"}


def case(method):
    return deepcopy(
        {
            CREATE: (P, {"packageName": APP}, None, {}),
            UPDATE: (P, UPDATE_BODY, None, {}),
            STATUS: (TARGET, {"publishState": "APP_STORE_APP_PUBLISH_STATE_UNPUBLISHED"}, None, {}),
            APK: (TARGET, {}, "application/vnd.android.package-archive", {"apkId": "apk-1"}),
            IMAGE: (TARGET, {}, "image/png", {"imageId": "image-1"}),
            POLICY: (
                TARGET,
                {"fileType": "DECLARATION_FILE_TYPE_DOCUMENT"},
                "application/pdf",
                {"fileId": "file-1"},
            ),
            GET: ({**P, "playAppPackageName": APP}, None, None, {"appView": {"packageName": APP}}),
            LIST: (
                RANGE,
                None,
                None,
                {
                    "recentUpdateEvents": [
                        {
                            "playAppPackageName": APP,
                            "eventTime": "2026-10-01T01:00:00Z",
                            "updateType": "DELETION",
                        }
                    ],
                    "nextPageToken": "opaque+/token",
                },
            ),
        }[method]
    )


@pytest.mark.parametrize("method", sorted(APPSTORE_METHODS))
def test_every_method_has_fixed_route_valid_request_and_response(method):
    p, body, mime, response = case(method)
    spec = request_spec(method, p, body, mime)
    assert spec["url"].startswith("https://androidpublisher.googleapis.com/")
    assert spec["target_package"] == (None if method == LIST else APP)
    validate_response(spec, response)
    if method == POLICY:
        assert spec["url"].endswith("?uploadType=multipart")
    elif mime:
        assert spec["url"].endswith("?uploadType=media")


@pytest.mark.parametrize(
    "method,parameters,body,mime",
    [
        (CREATE, P, {}, None),
        (CREATE, P, {"packageName": "../escape"}, None),
        (CREATE, {"appStorePackageName": "https://evil.test"}, {"packageName": APP}, None),
        (CREATE, P, {"packageName": APP, "extra": "PRIVATE"}, None),
        (UPDATE, P, {"packageName": APP}, None),
        (STATUS, TARGET, {"publishState": "APP_STORE_APP_PUBLISH_STATE_UNSPECIFIED"}, None),
        (POLICY, TARGET, {}, "application/pdf"),
        (POLICY, TARGET, {"fileType": "DECLARATION_FILE_TYPE_UNSPECIFIED"}, "application/pdf"),
        (IMAGE, TARGET, {}, "image/svg+xml"),
        (IMAGE, TARGET, {}, "image/png\r\nX: private"),
        (LIST, P, None, None),
        (LIST, {**RANGE, "pageSize": True}, None, None),
        (LIST, {**RANGE, "pageSize": 1001}, None, None),
        (LIST, {**RANGE, "endTime": RANGE["startTime"]}, None, None),
        (LIST, {**RANGE, "startTime": "2026-10-01"}, None, None),
        (LIST, {**RANGE, "startTime": "2026-02-30T00:00:00Z"}, None, None),
        (GET, {**P, "playAppPackageName": APP}, {}, None),
    ],
)
def test_invalid_intent_is_rejected(method, parameters, body, mime):
    with pytest.raises(PlayError):
        request_spec(method, parameters, body, mime)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b["appDetails"].pop("contactEmail"),
        lambda b: b["activeLocalizedStoreListings"][0].update(screenshotId=[]),
        lambda b: b["activeApks"]["activeApkSets"][0].update(alreadyPublishedOnPlay=True),
        lambda b: b["activeApks"]["activeApkSets"][0].update(versionCode="01"),
        lambda b: b["policyDeclarations"][0]["responses"][0].update(
            stringResponse={"value": "yes"}
        ),
        lambda b: b["policyDeclarations"][0]["responses"][0].update(booleanResponse={}),
        lambda b: b["activeLocalizedStoreListings"].append(
            deepcopy(b["activeLocalizedStoreListings"][0])
        ),
        lambda b: b["appDetails"].update(developerName="\ud800"),
    ],
)
def test_complete_update_required_fields_and_ambiguity(mutation):
    body = deepcopy(UPDATE_BODY)
    mutation(body)
    with pytest.raises(PlayError):
        request_spec(UPDATE, P, body)


@pytest.mark.parametrize(
    "method,response",
    [
        (APK, {}),
        (IMAGE, {"imageId": ""}),
        (POLICY, {"fileId": 5}),
        (CREATE, {"unexpected": True}),
        (GET, {"appView": {"packageName": STORE}}),
        (LIST, {"recentUpdateEvents": [{"updateType": "DELETION"}]}),
    ],
)
def test_missing_or_conflicting_provider_results_fail(method, response):
    p, body, mime, _ = case(method)
    with pytest.raises(PlayError):
        validate_response(request_spec(method, p, body, mime), response)


def test_nanosecond_catalog_bounds_are_preserved():
    params = {
        **RANGE,
        "startTime": "2026-10-01T00:00:00.000000001Z",
        "endTime": "2026-10-01T00:00:00.000000002Z",
    }
    spec = request_spec(LIST, params)
    event = {
        "playAppPackageName": APP,
        "eventTime": params["startTime"],
        "updateType": "MODIFICATION",
    }
    validate_response(spec, {"recentUpdateEvents": [event]})
    with pytest.raises(PlayError, match="outside"):
        validate_response(spec, {"recentUpdateEvents": [{**event, "eventTime": params["endTime"]}]})
    with pytest.raises(PlayError):
        request_spec(
            LIST, {**params, "startTime": params["endTime"], "endTime": params["startTime"]}
        )


@pytest.mark.parametrize(
    "response",
    [
        {
            "questionId": "q",
            "keyedGroupResponse": {
                "groups": [{"responses": [{"questionId": "n", "booleanResponse": {"value": True}}]}]
            },
        },
        {"questionId": "q", "keyedGroupResponse": {"groups": [{"key": "k"}]}},
        {"questionId": "q", "groupResponse": {"groups": [{"responses": [{}]}]}},
        {"questionId": "q", "groupResponse": {"groups": [{"responses": [{"questionId": "n"}]}]}},
        {
            "questionId": "q",
            "documentResponse": {
                "documentId": "d",
                "nonExpiring": True,
                "expiryDate": {"year": 2027, "month": 1, "day": 1},
            },
        },
        {"questionId": "q", "documentResponse": {"documentId": "d", "expiryDate": {"year": 2027}}},
    ],
)
def test_nested_policy_missing_required_and_conflicting_fields_fail_cleanly(response):
    body = deepcopy(UPDATE_BODY)
    body["policyDeclarations"][0]["responses"] = [response]
    with pytest.raises(PlayError):
        request_spec(UPDATE, P, body)


def test_catalog_timezone_offset_minutes_do_not_normalize_invalid_input():
    with pytest.raises(PlayError):
        request_spec(LIST, {**RANGE, "startTime": "2026-10-01T00:00:00+00:99"})
