import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from pubship.config import Settings
from pubship.contracts import methods
from pubship.coverage import PUBLISHER_READ_METHODS, SENSITIVE_PUBLISHER_READ_METHODS
from pubship.errors import PlayError
from pubship.publisher_reads import PublisherReads
from pubship.publisher_reads_contracts import validate_publisher_read

PACKAGE = "com.example.app"
PREFIX = "androidpublisher."
LIST = PREFIX + "monetization.subscriptions.list"
VOIDED = PREFIX + "purchases.voidedpurchases.list"
PARAMETERS = {"packageName": PACKAGE}


@pytest.fixture
def reader():
    return PublisherReads(
        Settings(packages=frozenset({PACKAGE}), sensitive_read_packages=frozenset({PACKAGE})),
        Mock(token=Mock(return_value="synthetic-token")),
        local=True,
    )


def test_allowlists_match_exact_pinned_get_contracts():
    def walk(node):
        yield from node.get("methods", {}).values()
        for resource in node.get("resources", {}).values():
            yield from walk(resource)

    source = Path(__file__).resolve().parents[1] / "api/discovery/androidpublisher.json"
    discovery = {row["id"]: row for row in walk(json.loads(source.read_text()))}
    assert len(PUBLISHER_READ_METHODS) == 19
    assert len(SENSITIVE_PUBLISHER_READ_METHODS) == 7
    assert not PUBLISHER_READ_METHODS & SENSITIVE_PUBLISHER_READ_METHODS
    for method_id in PUBLISHER_READ_METHODS | SENSITIVE_PUBLISHER_READ_METHODS:
        expected = discovery[method_id]
        assert expected["httpMethod"] == "GET" and "request" not in expected
        for key in ("httpMethod", "path", "parameters", "response", "scopes"):
            assert methods()[method_id][key] == expected[key]


def test_one_page_preserves_fields_missing_values_and_untrusted_links(reader):
    data = {
        "subscriptions": [{"productId": "plan", "basePlans": [{"state": "DRAFT"}]}],
        "nextPageToken": "next-private-page",
        "unknown": {"url": "https://untrusted.invalid/do-not-fetch"},
    }
    with patch("pubship.http.get", return_value=data) as get:
        result = reader.read(LIST, PARAMETERS)
    assert result["data"] is data
    assert "total" not in result["data"]
    assert result["data_scope"] == "publisher_response" and not result["sensitive"]
    assert datetime.fromisoformat(result["fetched_at"]).tzinfo is not None
    get.assert_called_once()
    reader.auth.token.assert_called_once_with("publisher")
    assert "no automatic pagination" in result["note"]
    assert "parameters" not in result and "url" not in result


@pytest.mark.parametrize("value", [None, [], "PRIVATE", 1, True])
def test_non_object_response_is_redacted(reader, value):
    with patch("pubship.http.get", return_value=value), pytest.raises(PlayError) as error:
        reader.read(LIST, PARAMETERS)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "method,parameters",
    [
        (LIST, None),
        (LIST, []),
        (LIST, {"packageName": "com.other.app"}),
        (LIST, {**PARAMETERS, "privateUnknown": "PRIVATE"}),
        (LIST, {**PARAMETERS, "pageSize": True}),
        (LIST, {**PARAMETERS, "pageSize": 1.0}),
        (LIST, {**PARAMETERS, "pageSize": 0}),
        (LIST, {**PARAMETERS, "pageSize": 1001}),
        (LIST, {**PARAMETERS, "pageToken": "x" * 4097}),
        (LIST, {**PARAMETERS, "pageToken": "\ud800"}),
        (LIST, {**PARAMETERS, "pageToken": "PRIVATE\n"}),
        (LIST, {**PARAMETERS, "pageToken": "PRIVATE\u0085"}),
        (LIST, {**PARAMETERS, "showArchived": 1}),
        (PREFIX + "applications.deviceTierConfigs.list", {**PARAMETERS, "pageSize": 101}),
        (PREFIX + "applications.deviceTierConfigs.get", {**PARAMETERS, "deviceTierConfigId": "-1"}),
        (
            PREFIX + "applications.deviceTierConfigs.get",
            {**PARAMETERS, "deviceTierConfigId": "9223372036854775808"},
        ),
        (PREFIX + "apprecovery.list", PARAMETERS),
        (PREFIX + "apprecovery.list", {**PARAMETERS, "versionCode": "0"}),
        (PREFIX + "apprecovery.list", {**PARAMETERS, "versionCode": 1}),
        (PREFIX + "generatedapks.list", {**PARAMETERS, "versionCode": 0}),
        (PREFIX + "generatedapks.list", {**PARAMETERS, "versionCode": 2**31}),
        (PREFIX + "generatedapks.list", {**PARAMETERS, "versionCode": 1.0}),
        (
            PREFIX + "systemapks.variants.get",
            {**PARAMETERS, "versionCode": "1", "variantId": 2**32},
        ),
        (PREFIX + "systemapks.variants.get", {**PARAMETERS, "versionCode": "1", "variantId": -1}),
        (PREFIX + "inappproducts.get", {**PARAMETERS, "sku": "../PRIVATE"}),
        (
            PREFIX + "edits.expansionfiles.get",
            {
                **PARAMETERS,
                "editId": "edit",
                "apkVersionCode": 1,
                "expansionFileType": "expansionFileTypeUnspecified",
            },
        ),
        (
            PREFIX + "edits.expansionfiles.get",
            {
                **PARAMETERS,
                "editId": "edit/PRIVATE",
                "apkVersionCode": 1,
                "expansionFileType": "main",
            },
        ),
        (PREFIX + "edits.commit", PARAMETERS),
        (PREFIX + "orders.get", {**PARAMETERS, "orderId": "PRIVATE"}),
        ("https://evil.invalid/PRIVATE", PARAMETERS),
    ],
)
def test_invalid_ordinary_requests_fail_before_auth_and_provider(reader, method, parameters):
    with patch("pubship.http.get") as get, pytest.raises(PlayError) as error:
        reader.read(method, parameters)
    assert "PRIVATE" not in str(error.value)
    reader.auth.token.assert_not_called()
    get.assert_not_called()


@pytest.mark.parametrize(
    "method,field,limit",
    [
        (PREFIX + "inappproducts.batchGet", "sku", 100),
        (PREFIX + "monetization.subscriptions.batchGet", "productIds", 100),
        (PREFIX + "monetization.onetimeproducts.batchGet", "productIds", 100),
        (PREFIX + "orders.batchget", "orderIds", 1000),
    ],
)
def test_batches_are_explicit_bounded_distinct_and_repeated(method, field, limit):
    sensitive = method in SENSITIVE_PUBLISHER_READ_METHODS
    for value in (None, [], ["same", "same"], [f"id{i}" for i in range(limit + 1)]):
        parameters = {**PARAMETERS, **({field: value} if value is not None else {})}
        with pytest.raises(PlayError):
            validate_publisher_read(method, parameters, sensitive=sensitive)
    values = [f"id{i}" for i in range(limit)]
    package, url = validate_publisher_read(
        method, {**PARAMETERS, field: values}, sensitive=sensitive
    )
    assert package == PACKAGE
    assert parse_qs(urlsplit(url).query)[field] == values


@pytest.mark.parametrize(
    "method,parent",
    [
        (PREFIX + "monetization.subscriptions.basePlans.offers.list", "basePlanId"),
        (PREFIX + "monetization.onetimeproducts.purchaseOptions.offers.list", "purchaseOptionId"),
    ],
)
def test_all_product_offer_queries_require_the_matching_parent_wildcard(method, parent):
    with pytest.raises(PlayError):
        validate_publisher_read(method, {**PARAMETERS, "productId": "-", parent: "specific"})
    for product, child in (("-", "-"), ("specific", "-"), ("specific", "child")):
        _, url = validate_publisher_read(
            method, {**PARAMETERS, "productId": product, parent: child}
        )
        assert "{" not in url


def test_query_tokens_remain_exact_and_booleans_use_wire_lowercase():
    token = "next+/=&?é"
    _, url = validate_publisher_read(
        LIST, {**PARAMETERS, "pageToken": token, "showArchived": False, "pageSize": 1000}
    )
    assert parse_qs(urlsplit(url).query) == {
        "pageToken": [token],
        "showArchived": ["false"],
        "pageSize": ["1000"],
    }


def test_legacy_ignored_pagination_arguments_are_not_claimed_to_bound_result(reader):
    with patch("pubship.http.get", return_value={}) as get:
        result = reader.read(
            PREFIX + "inappproducts.list",
            {**PARAMETERS, "maxResults": 1, "startIndex": 0, "token": ""},
        )
    assert "ignored by Google" in result["note"]
    assert parse_qs(urlsplit(get.call_args.args[0]).query, keep_blank_values=True) == {
        "maxResults": ["1"],
        "startIndex": ["0"],
        "token": [""],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"type": 2},
        {"type": True},
        {"maxResults": 0},
        {"maxResults": 1001},
        {"startTime": "-1"},
        {"endTime": "9223372036854775808"},
        {"startTime": "2", "endTime": "1"},
        {"includeQuantityBasedPartialRefund": "true"},
    ],
)
def test_invalid_sensitive_query_fails_before_auth(reader, changes):
    with patch("pubship.http.get") as get, pytest.raises(PlayError):
        reader.read_sensitive(VOIDED, {**PARAMETERS, **changes})
    reader.auth.token.assert_not_called()
    get.assert_not_called()


def test_voided_pagination_cannot_bypass_disabled_method(reader):
    with patch("pubship.http.get") as get, pytest.raises(PlayError):
        reader.read_sensitive(
            VOIDED, {**PARAMETERS, "token": "synthetic-page", "startTime": "2", "endTime": "1"}
        )
    get.assert_not_called()
    reader.auth.token.assert_not_called()


def test_edit_tester_results_are_sensitive_snapshots_and_tokens_are_not_echoed(reader):
    data = {"googleGroups": ["private-group@example.invalid"]}
    with patch("pubship.http.get", return_value=data):
        result = reader.read_sensitive(
            PREFIX + "edits.testers.get",
            {**PARAMETERS, "editId": "PRIVATE-edit", "track": "wear:production"},
        )
    assert result["data"] == data and result["data_scope"] == "edit_snapshot"
    assert "PRIVATE-edit" not in json.dumps(result)


def test_sensitive_scope_from_env_does_not_grant_mutations(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", PACKAGE)
    monkeypatch.setenv("GOOGLE_PLAY_SENSITIVE_READ_PACKAGES", " " + PACKAGE + " ")
    for name in ("WRITE", "EDIT", "TRACK"):
        monkeypatch.delenv("GOOGLE_PLAY_" + name + "_PACKAGES", raising=False)
    settings = Settings.from_env()
    assert settings.sensitive_read_packages == frozenset({PACKAGE})
    assert (
        not settings.write_packages and not settings.edit_packages and not settings.track_packages
    )
    for value in ({PACKAGE}, frozenset({"com.other.app"}), frozenset({"INVALID"})):
        with pytest.raises(PlayError, match="SENSITIVE_READ_PACKAGES"):
            Settings(packages=frozenset({PACKAGE}), sensitive_read_packages=value)


def test_sensitive_ids_allow_encoded_punctuation_but_no_paths():
    method = PREFIX + "orders.get"
    for invalid in ("..", ".", "PRIVATE/other", "PRIVATE\\other", "%2FPRIVATE", "PRIVATE\n"):
        with pytest.raises(PlayError):
            validate_publisher_read(method, {**PARAMETERS, "orderId": invalid}, sensitive=True)
    _, url = validate_publisher_read(
        method, {**PARAMETERS, "orderId": "PRIVATE+id=é?"}, sensitive=True
    )
    assert url.endswith("/orders/PRIVATE%2Bid%3D%C3%A9%3F")


def test_read_requires_explicit_local_service_and_sensitive_optin():
    auth = Mock()
    settings = Settings(packages=frozenset({PACKAGE}))
    with pytest.raises(PlayError, match="local services"):
        PublisherReads(settings, auth).read(LIST, PARAMETERS)
    with pytest.raises(PlayError, match="SENSITIVE_READ_PACKAGES"):
        PublisherReads(settings, auth, local=True).read_sensitive(
            PREFIX + "orders.get", {**PARAMETERS, "orderId": "PRIVATE-order"}
        )
    auth.token.assert_not_called()
