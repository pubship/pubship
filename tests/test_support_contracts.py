"""Synthetic support-write contract checks; no credentials or provider writes."""

from copy import deepcopy

import pytest

from pubship.errors import PlayError
from pubship.support_contracts import (
    SUPPORT_METHODS,
    request_spec,
    validate_mutation_response,
    validate_targeting,
    validate_timestamp,
)

P = "androidpublisher."
PACKAGE = {"packageName": "com.example.app"}
TARGETING = {"allUsers": {"isAllUsersRequested": True}, "versionList": {"versionCodes": ["42"]}}
DEVICE = {"screenDensity": 480, "supportedAbis": ["arm64-v8a"], "supportedLocales": ["en-US"]}
CONFIG = {
    "deviceGroups": [
        {"name": "large", "deviceSelectors": [{"deviceRam": {"minBytes": "4294967296"}}]}
    ],
    "deviceTierSet": {"deviceTiers": [{"level": 1, "deviceGroupNames": ["large"]}]},
    "userCountrySets": [{"name": "north", "countryCodes": ["US", "CA"]}],
}
CASES = [
    (
        "reviews.reply",
        {**PACKAGE, "reviewId": "review-1"},
        {"replyText": "Thank you!"},
        "/reviews/review-1:reply",
    ),
    (
        "applications.dataSafety",
        PACKAGE,
        {"safetyLabels": "Question ID,Response value\nexample,TRUE\n"},
        "/dataSafety",
    ),
    (
        "applications.deviceTierConfigs.create",
        {**PACKAGE, "allowUnknownDevices": False},
        CONFIG,
        "/deviceTierConfigs?allowUnknownDevices=false",
    ),
    (
        "apprecovery.create",
        PACKAGE,
        {"targeting": TARGETING, "remoteInAppUpdate": {"isRemoteInAppUpdateRequested": True}},
        "/appRecoveries",
    ),
    (
        "apprecovery.addTargeting",
        {**PACKAGE, "appRecoveryId": "7"},
        {"targetingUpdate": {"regions": {"regionCode": ["US"]}}},
        "/appRecoveries/7:addTargeting",
    ),
    ("apprecovery.cancel", {**PACKAGE, "appRecoveryId": "7"}, {}, "/appRecoveries/7:cancel"),
    ("apprecovery.deploy", {**PACKAGE, "appRecoveryId": "7"}, {}, "/appRecoveries/7:deploy"),
    (
        "systemapks.variants.create",
        {**PACKAGE, "versionCode": "42"},
        {"deviceSpec": DEVICE, "options": {"rotated": False}},
        "/systemApks/42/variants",
    ),
]


@pytest.mark.parametrize("name,parameters,body,suffix", CASES)
def test_fixed_routes_and_preserved_intent(name, parameters, body, suffix):
    supplied_parameters, supplied_body = deepcopy(parameters), deepcopy(body)
    spec = request_spec(P + name, supplied_parameters, supplied_body)
    assert (
        spec["url"]
        == "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.example.app"
        + suffix
    )
    assert spec["http_method"] == "POST"
    assert spec["parameters"] == parameters
    assert spec["body"] == body
    assert spec["effects"]
    supplied_body["changed"] = True
    supplied_parameters["packageName"] = "com.other.app"
    assert spec["body"] == body
    assert spec["parameters"] == parameters


def test_exact_capability_inventory():
    assert SUPPORT_METHODS == {P + case[0] for case in CASES}
    for method in (P + "reviews.get", P + "edits.commit", "https://example.org", None, []):
        with pytest.raises(PlayError):
            request_spec(method, PACKAGE, {})


@pytest.mark.parametrize("name,parameters,body,suffix", CASES)
def test_unknown_fields_and_cross_app_bodies_fail(name, parameters, body, suffix):
    for unknown in (
        {**parameters, "url": "https://example.org"},
        {**parameters, "packageName": "com.example/app"},
    ):
        with pytest.raises(PlayError):
            request_spec(P + name, unknown, body)
    with pytest.raises(PlayError):
        request_spec(P + name, parameters, {**body, "packageName": "com.other.app"})
    with pytest.raises(PlayError):
        request_spec(P + name, parameters, None)


@pytest.mark.parametrize("value", ["", " ", "a" * 351, "\ud800", "reply\x00text", None])
def test_reply_requires_operator_text(value):
    with pytest.raises(PlayError):
        request_spec(P + "reviews.reply", {**PACKAGE, "reviewId": "r1"}, {"replyText": value})


@pytest.mark.parametrize(
    "review_id", ["..", "a/b", "a%2Fb", "r?other=x", "https://elsewhere", "a" * 1025]
)
def test_review_path_injection_rejected(review_id):
    with pytest.raises(PlayError):
        request_spec(
            P + "reviews.reply", {**PACKAGE, "reviewId": review_id}, {"replyText": "Hello"}
        )


@pytest.mark.parametrize("value", ["0", "01", "-1", "9223372036854775808", "1/2", 1, True])
def test_recovery_id_must_be_canonical_positive_int64(value):
    with pytest.raises(PlayError):
        request_spec(P + "apprecovery.deploy", {**PACKAGE, "appRecoveryId": value}, {})


@pytest.mark.parametrize(
    "targeting",
    [
        {},
        {"allUsers": {}},
        {**TARGETING, "regions": {"regionCode": ["US"]}},
        {**TARGETING, "versionRange": {"versionCodeStart": "1", "versionCodeEnd": "5"}},
        {**TARGETING, "versionList": {"versionCodes": []}},
        {**TARGETING, "versionList": {"versionCodes": ["1", "1"]}},
        {"allUsers": {"isAllUsersRequested": False}, "versionList": {"versionCodes": ["42"]}},
        {"regions": {"regionCode": ["us"]}, "versionList": {"versionCodes": ["42"]}},
        {"androidSdks": {"sdkLevels": ["0"]}, "versionList": {"versionCodes": ["42"]}},
        {
            "allUsers": {"isAllUsersRequested": True},
            "versionRange": {"versionCodeStart": "2", "versionCodeEnd": "1"},
        },
    ],
)
def test_recovery_targeting_unions_and_nested_values(targeting):
    with pytest.raises(PlayError):
        request_spec(
            P + "apprecovery.create",
            PACKAGE,
            {"targeting": targeting, "remoteInAppUpdate": {"isRemoteInAppUpdateRequested": True}},
        )


@pytest.mark.parametrize("action", [{}, {"isRemoteInAppUpdateRequested": False}])
def test_recovery_cannot_infer_action(action):
    with pytest.raises(PlayError):
        request_spec(
            P + "apprecovery.create", PACKAGE, {"targeting": TARGETING, "remoteInAppUpdate": action}
        )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"deviceTierConfigId": "1"},
        {**CONFIG, "deviceTierConfigId": "2"},
        {"deviceGroups": [{"name": "missing"}]},
        {"deviceGroups": [{"name": "all", "deviceSelectors": [{}]}]},
        {
            **CONFIG,
            "deviceTierSet": {
                "deviceTiers": [{"level": 1, "deviceGroupNames": ["other-app/group"]}]
            },
        },
        {**CONFIG, "deviceTierSet": {"deviceTiers": [{"level": 0, "deviceGroupNames": ["large"]}]}},
        {
            **CONFIG,
            "deviceTierSet": {"deviceTiers": [{"level": 1.0, "deviceGroupNames": ["large"]}]},
        },
        {**CONFIG, "deviceTierSet": {"deviceTiers": [{"level": 2, "deviceGroupNames": ["large"]}]}},
        {"deviceGroups": CONFIG["deviceGroups"] * 2},
        {"userCountrySets": [{"name": "north", "countryCodes": ["US", "US"]}]},
        {
            "deviceGroups": [
                {
                    "name": "bad",
                    "deviceSelectors": [{"deviceRam": {"minBytes": "9", "maxBytes": "8"}}],
                }
            ]
        },
        {
            "deviceGroups": [
                {"name": "bad", "deviceSelectors": [{"systemOnChips": [{"model": "Tensor"}]}]}
            ]
        },
    ],
)
def test_device_configuration_validation(body):
    with pytest.raises(PlayError):
        request_spec(P + "applications.deviceTierConfigs.create", PACKAGE, body)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"variantId": 7, "deviceSpec": DEVICE},
        {"deviceSpec": {}},
        {"deviceSpec": {**DEVICE, "screenDensity": 0}},
        {"deviceSpec": {**DEVICE, "screenDensity": True}},
        {"deviceSpec": {**DEVICE, "screenDensity": 2**32}},
        {"deviceSpec": {**DEVICE, "supportedAbis": []}},
        {"deviceSpec": {**DEVICE, "supportedLocales": ["en", "en"]}},
        {"deviceSpec": {**DEVICE, "packageName": "com.other.app"}},
    ],
)
def test_system_variant_validation(body):
    with pytest.raises(PlayError):
        request_spec(P + "systemapks.variants.create", {**PACKAGE, "versionCode": "42"}, body)


def test_readonly_content_is_not_generated_or_normalized():
    body = {"replyText": "<b>Thank you</b> 🙂"}
    spec = request_spec(P + "reviews.reply", {**PACKAGE, "reviewId": "r1"}, body)
    assert spec["body"] == body
    validate_mutation_response(
        spec, {"result": {"replyText": "Thank you 🙂", "lastEdited": {"seconds": "1700000000"}}}
    )


@pytest.mark.parametrize("index", [1, 4, 5, 6])
def test_documented_empty_success_responses(index):
    name, parameters, body, _ = CASES[index]
    spec = request_spec(P + name, parameters, body)
    assert spec["empty_response"] is True
    validate_mutation_response(spec, {})
    for malformed in (None, [], {"success": True}):
        with pytest.raises(PlayError):
            validate_mutation_response(spec, malformed)


@pytest.mark.parametrize(
    "index,response",
    [
        (
            0,
            {
                "result": {
                    "replyText": "Thank you!",
                    "lastEdited": {"seconds": "1700000000", "nanos": 3},
                }
            },
        ),
        (2, {**CONFIG, "deviceTierConfigId": "5"}),
        (3, {"appRecoveryId": "7", "status": "RECOVERY_STATUS_DRAFT", "targeting": TARGETING}),
        (7, {"variantId": 2, "deviceSpec": DEVICE}),
    ],
)
def test_created_resources_and_reply_require_evidence(index, response):
    name, parameters, body, _ = CASES[index]
    spec = request_spec(P + name, parameters, body)
    assert spec["empty_response"] is False
    validate_mutation_response(spec, response)
    for malformed in (None, [], {}, {**response, "packageName": "com.other.app"}):
        with pytest.raises(PlayError):
            validate_mutation_response(spec, malformed)


def test_recovery_creation_mismatched_targeting_is_unknown():
    name, parameters, body, _ = CASES[3]
    spec = request_spec(P + name, parameters, body)
    with pytest.raises(PlayError):
        validate_mutation_response(
            spec,
            {
                "appRecoveryId": "7",
                "status": "RECOVERY_STATUS_DRAFT",
                "targeting": {**TARGETING, "versionList": {"versionCodes": ["99"]}},
            },
        )


def test_payload_size_and_nonfinite_rejected():
    for body in (
        {"safetyLabels": "x" * 65536},
        {"safetyLabels": float("nan")},
        {"safetyLabels": ""},
    ):
        with pytest.raises(PlayError):
            request_spec(P + "applications.dataSafety", PACKAGE, body)


@pytest.mark.parametrize(
    "timestamp",
    [
        {},
        {"seconds": "not-time"},
        {"seconds": "9223372036854775808"},
        {"seconds": "1700000000", "nanos": -1},
        {"seconds": "1700000000", "nanos": 1000000000},
        {"seconds": "1700000000", "nanos": 1.0},
    ],
)
def test_review_response_requires_valid_timestamp(timestamp):
    spec = request_spec(P + "reviews.reply", {**PACKAGE, "reviewId": "r1"}, {"replyText": "Hello"})
    with pytest.raises(PlayError):
        validate_mutation_response(
            spec, {"result": {"replyText": "Hello", "lastEdited": timestamp}}
        )


@pytest.mark.parametrize(
    "targeting",
    [
        {**TARGETING, "allUsers": {"isAllUsersRequested": False}},
        {**TARGETING, "allUsers": {}},
        {"versionList": {"versionCodes": ["42"]}, "regions": {"regionCode": []}},
        {"versionList": {"versionCodes": ["42"]}, "androidSdks": {"sdkLevels": []}},
        {**TARGETING, "versionList": {"versionCodes": ["42", "42"]}},
        {**TARGETING, "allUsers": None},
        {**TARGETING, "packageName": "com.other.app"},
    ],
)
def test_public_observed_targeting_validation(targeting):
    with pytest.raises(PlayError):
        validate_targeting(targeting)


@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        [],
        {},
        {"seconds": "not-time"},
        {"seconds": "9223372036854775808"},
        {"seconds": "1700000000", "nanos": -1},
        {"seconds": "1700000000", "nanos": 1000000000},
        {"seconds": "1700000000", "nanos": 1.0},
        {"seconds": "1700000000", "nanos": True},
        {"seconds": "1700000000", "extra": "unexpected"},
    ],
)
def test_public_observed_timestamp_validation(timestamp):
    with pytest.raises(PlayError):
        validate_timestamp(timestamp)


def test_public_provider_validators_accept_explicit_data():
    validate_targeting(TARGETING)
    validate_targeting({"allUsers": {"isAllUsersRequested": True}}, update=True)
    validate_timestamp({"seconds": "1700000000"})
    validate_timestamp({"seconds": "1700000000", "nanos": 999999999})
