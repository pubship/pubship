import asyncio
import io
import json
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest
from jsonschema import Draft202012Validator
from mcp import Client

from pubship import http
from pubship.config import Settings
from pubship.contracts import describe_method, methods, request_schema, validate_request
from pubship.coverage import REPORTING_METHODS
from pubship.errors import PlayError
from pubship.reporting import Reporting
from pubship.server import create_server

PACKAGE = "com.example.app"
QUERY = "playdeveloperreporting.vitals.crashrate.query"


def response_context(content):
    context = Mock()
    context.__enter__ = Mock(return_value=Mock(read=Mock(return_value=content)))
    context.__exit__ = Mock(return_value=False)
    return context


def parameters_for(method):
    result = {}
    for key, schema in method.get("parameters", {}).items():
        if schema.get("location") == "path":
            result[key] = (
                schema["pattern"].removeprefix("^").removesuffix("$").replace("[^/]+", PACKAGE)
            )
    return result


@pytest.mark.parametrize("method_id", sorted(REPORTING_METHODS))
def test_every_reporting_contract_routes_and_preserves_provider_data(method_id):
    method = methods()[method_id]
    parameters = parameters_for(method)
    body = {"pageSize": 50, "pageToken": "synthetic-page"} if "request" in method else None
    if method_id.endswith("apps.search"):
        data = {"apps": [{"name": "apps/" + PACKAGE}], "nextPageToken": "synthetic-next"}
    else:
        data = {"rows": [], "nextPageToken": "synthetic-next", "freshnessInfo": {"freshnesses": []}}
    auth = Mock()
    auth.token.return_value = "synthetic"
    reporting = Reporting(Settings(packages=frozenset({PACKAGE})), auth)
    with (
        patch("pubship.http.get", return_value=data) as get,
        patch("pubship.http.reporting_query", return_value=data) as query,
    ):
        result = reporting.read(method_id, parameters, body)
    assert result["data"] == data
    assert result["method"] == method_id
    assert "missing rows are not zero" in result["note"]
    auth.token.assert_called_once_with("reporting")
    called, unused = (get, query) if method["httpMethod"] == "GET" else (query, get)
    called.assert_called_once()
    unused.assert_not_called()
    target = urlsplit(called.call_args.args[0])
    assert target.scheme == "https"
    assert target.netloc == "playdeveloperreporting.googleapis.com"
    assert "{" not in target.path
    if "request" in method:
        assert called.call_args.args[2] == body
    schema = request_schema(method_id)
    Draft202012Validator.check_schema(schema)


def test_reporting_coverage_exactly_matches_pinned_discovery():
    def walk(node):
        yield from node.get("methods", {}).values()
        for resource in node.get("resources", {}).values():
            yield from walk(resource)

    source = Path(__file__).resolve().parents[1] / "api/discovery/playdeveloperreporting.json"
    discovery = {method["id"]: method for method in walk(json.loads(source.read_text()))}
    assert set(discovery) == REPORTING_METHODS
    for method_id, method in discovery.items():
        for key in ("httpMethod", "path", "parameters", "request", "response", "scopes"):
            assert methods()[method_id].get(key) == method.get(key)


@pytest.mark.parametrize("method_id", sorted(REPORTING_METHODS))
def test_every_reporting_method_reaches_the_actual_transport(method_id):
    method = methods()[method_id]
    parameters = parameters_for(method)
    body = {"pageSize": 1, "pageToken": "synthetic-page"} if "request" in method else None
    context = response_context(b'{"nextPageToken":"synthetic-next"}')
    auth = Mock()
    auth.token.return_value = "synthetic"
    with patch("urllib.request.OpenerDirector.open", return_value=context) as request:
        result = Reporting(Settings(packages=frozenset({PACKAGE})), auth).read(
            method_id, parameters, body
        )
    sent = request.call_args.args[0]
    expected_path = method["path"]
    for name, value in parameters.items():
        expected_path = expected_path.replace("{+" + name + "}", value)
    assert sent.full_url == "https://playdeveloperreporting.googleapis.com/" + expected_path
    assert sent.get_method() == method["httpMethod"]
    assert sent.get_header("Authorization") == "Bearer synthetic"
    assert result["data"]["nextPageToken"] == "synthetic-next"
    if body is not None:
        assert json.loads(sent.data) == body
    else:
        assert sent.data is None


@pytest.mark.parametrize(
    "resource",
    [
        "apps/com.other.app/crashRateMetricSet",
        "apps/com.example.app/../crashRateMetricSet",
        "apps/com.example.app%2f../crashRateMetricSet",
        "https://evil.example/crashRateMetricSet",
    ],
)
def test_scope_and_path_fail_before_authentication(resource):
    auth = Mock()
    reporting = Reporting(Settings(packages=frozenset({PACKAGE})), auth)
    with pytest.raises(PlayError), patch("pubship.http.reporting_query") as query:
        reporting.read(QUERY, {"name": resource}, {})
    auth.token.assert_not_called()
    query.assert_not_called()


@pytest.mark.parametrize(
    "parameters,body",
    [
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet", "access_token": "PRIVATE"}, {}),
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, {"unknown": "PRIVATE"}),
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, {"pageSize": True}),
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, {"pageSize": 1001}),
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, {"pageToken": "x" * 4097}),
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, {"filter": "x" * 65536}),
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, {"userCohort": "PRIVATE"}),
        ({"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, None),
    ],
)
def test_invalid_inputs_are_bounded_private_and_do_not_authenticate(parameters, body):
    auth = Mock()
    with pytest.raises(PlayError) as error:
        Reporting(Settings(packages=frozenset({PACKAGE})), auth).read(QUERY, parameters, body)
    auth.token.assert_not_called()
    assert "PRIVATE" not in str(error.value)


def test_realistic_metric_query_preserves_timeline_units_and_missing_values():
    request = {
        "timelineSpec": {
            "aggregationPeriod": "DAILY",
            "startTime": {"year": 2026, "month": 9, "day": 1},
            "endTime": {"year": 2026, "month": 9, "day": 3},
        },
        "metrics": ["crashRate", "distinctUsers"],
        "dimensions": ["versionCode"],
        "pageSize": 100,
    }
    data = {"rows": [{"metrics": [{"metric": "crashRate", "decimalValue": {"value": "0.123"}}]}]}
    with patch("pubship.http.reporting_query", return_value=data) as query:
        result = Reporting(Settings(packages=frozenset({PACKAGE})), Mock()).read(
            QUERY, {"name": "apps/" + PACKAGE + "/crashRateMetricSet"}, request
        )
    assert query.call_args.args[2] == request
    assert result["data"] == data
    assert "distinctUsers" not in str(result["data"])


def test_apps_are_filtered_without_losing_the_next_page():
    data = {
        "apps": [{"name": "apps/com.other.app", "displayName": "PRIVATE"}],
        "nextPageToken": "page-two",
    }
    with patch("pubship.http.get", return_value=data) as get:
        result = Reporting(Settings(packages=frozenset({PACKAGE})), Mock()).read(
            "playdeveloperreporting.apps.search", {"pageSize": 5, "pageToken": "p+/="}
        )
    assert result["data"] == {"apps": [], "nextPageToken": "page-two"}
    assert "PRIVATE" not in str(result)
    assert parse_qs(urlsplit(get.call_args.args[0]).query) == {
        "pageSize": ["5"],
        "pageToken": ["p+/="],
    }


def test_app_search_requires_explicit_scope_before_auth():
    auth = Mock()
    with pytest.raises(PlayError):
        Reporting(Settings(), auth).read("playdeveloperreporting.apps.search", {})
    auth.token.assert_not_called()


@pytest.mark.parametrize("name", [None, {}, [], 123])
def test_malformed_app_resource_names_are_redacted(name):
    with (
        patch("pubship.http.get", return_value={"apps": [{"name": name}]}),
        pytest.raises(PlayError, match="unexpected app list"),
    ):
        Reporting(Settings(packages=frozenset({PACKAGE})), Mock()).read(
            "playdeveloperreporting.apps.search", {}
        )


def test_integral_float_query_parameters_use_integer_url_representation():
    with patch("pubship.http.get", return_value={}) as get:
        Reporting(Settings(packages=frozenset({PACKAGE})), Mock()).read(
            "playdeveloperreporting.vitals.errors.issues.search",
            {
                "parent": "apps/" + PACKAGE,
                "interval.startTime.year": 2026.0,
                "pageSize": 10.0,
                "sampleErrorReportLimit": 1.0,
            },
        )
    assert parse_qs(urlsplit(get.call_args.args[0]).query) == {
        "interval.startTime.year": ["2026"],
        "pageSize": ["10"],
        "sampleErrorReportLimit": ["1"],
    }


@pytest.mark.parametrize("value", [-(2**31) - 1, 2**31])
def test_discovery_int32_bounds_fail_before_authentication(value):
    auth = Mock()
    with pytest.raises(PlayError, match="pinned contract"):
        Reporting(Settings(packages=frozenset({PACKAGE})), auth).read(
            "playdeveloperreporting.vitals.errors.issues.search",
            {"parent": "apps/" + PACKAGE, "interval.startTime.year": value},
        )
    auth.token.assert_not_called()


def test_body_integer_bounds_and_format_are_retained():
    with pytest.raises(PlayError, match="pinned contract"):
        validate_request(
            QUERY,
            {"name": "apps/" + PACKAGE + "/crashRateMetricSet"},
            {"timelineSpec": {"startTime": {"year": 2**31}}},
        )
    schema = request_schema(QUERY)
    year = schema["$defs"]["GoogleTypeDateTime"]["properties"]["year"]
    assert year["format"] == "int32"
    assert year["minimum"] == -(2**31)
    assert year["maximum"] == 2**31 - 1


def test_other_apis_cannot_be_executed_through_reporting():
    auth = Mock()
    with pytest.raises(PlayError):
        Reporting(Settings(), auth).read("androidpublisher.edits.commit", {})
    auth.token.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "https://androidpublisher.googleapis.com/edits:commit",
        "https://evil.example/query",
        "https://playdeveloperreporting.googleapis.com/v1beta1/apps/com.example.app/xMetricSet:query?access_token=PRIVATE",
        "https://playdeveloperreporting.googleapis.com/v1beta1/apps/com.example.app/../xMetricSet:query",
    ],
)
def test_post_transport_cannot_be_used_for_writes_or_other_hosts(url):
    with patch("urllib.request.OpenerDirector.open") as request, pytest.raises(PlayError):
        http.reporting_query(url, "synthetic", {})
    request.assert_not_called()


def test_post_query_serialization_timeout_and_response_bound():
    response = Mock()
    response.read.return_value = b'{"rows":[]}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    with patch("urllib.request.OpenerDirector.open", return_value=context) as request:
        result = http.reporting_query(
            "https://playdeveloperreporting.googleapis.com/v1beta1/apps/com.example.app/crashRateMetricSet:query",
            "synthetic",
            {"pageSize": 10},
        )
    assert result == {"rows": []}
    assert request.call_args.args[0].get_method() == "POST"
    assert json.loads(request.call_args.args[0].data) == {"pageSize": 10}
    assert request.call_args.kwargs["timeout"] == 15
    response.read.assert_called_once_with(http.MAX_BYTES + 1)


def test_reporting_errors_do_not_expose_provider_body_or_token():
    error = HTTPError("https://example/PRIVATE", 403, "PRIVATE", {}, io.BytesIO(b"PRIVATE"))
    with (
        patch("urllib.request.OpenerDirector.open", side_effect=error),
        pytest.raises(PlayError) as raised,
    ):
        http.reporting_query(
            "https://playdeveloperreporting.googleapis.com/v1beta1/apps/com.example.app/crashRateMetricSet:query",
            "synthetic",
            {},
        )
    assert "403" in str(raised.value)
    assert "PRIVATE" not in str(raised.value)
    assert error.closed


@pytest.mark.parametrize(
    "payload",
    [
        b'{"PRIVATE":NaN}',
        b'{"PRIVATE":Infinity}',
        b'{"PRIVATE":-Infinity}',
        b'{"PRIVATE":1e999}',
    ],
)
def test_non_finite_responses_are_redacted(payload):
    with (
        patch("urllib.request.OpenerDirector.open", return_value=response_context(payload)),
        pytest.raises(PlayError) as error,
    ):
        http.get("https://playdeveloperreporting.googleapis.com/v1beta1/apps:search", "synthetic")
    assert "PRIVATE" not in str(error.value)
    assert "invalid data" in str(error.value)


def test_json_parser_recursion_errors_are_redacted():
    # Python versions differ in the nesting limit of their JSON decoder.
    with (
        patch("urllib.request.OpenerDirector.open", return_value=response_context(b"{}")),
        patch("pubship.http.json.loads", side_effect=RecursionError("PRIVATE")),
        pytest.raises(PlayError) as error,
    ):
        http.get("https://playdeveloperreporting.googleapis.com/v1beta1/apps:search", "synthetic")
    assert "PRIVATE" not in str(error.value)
    assert "invalid data" in str(error.value)


def test_truncated_http_response_is_redacted():
    context = response_context(b"")
    context.__enter__.return_value.read.side_effect = IncompleteRead(b"PRIVATE", 100)
    with (
        patch("urllib.request.OpenerDirector.open", return_value=context),
        pytest.raises(PlayError, match="request failed") as error,
    ):
        http.get("https://playdeveloperreporting.googleapis.com/v1beta1/apps:search", "synthetic")
    assert "PRIVATE" not in str(error.value)
    context.__exit__.assert_called_once()


def test_describe_method_has_only_reachable_definitions_and_no_execution():
    result = describe_method(QUERY)
    schema = result["input_schema"]
    Draft202012Validator.check_schema(schema)
    assert 1 < len(schema["$defs"]) < 58
    assert result["method"]["status"] == "implemented"
    assert describe_method("androidpublisher.edits.commit")["method"]["status"] == "implemented"
    assert (
        "GOOGLE_PLAY_EDIT_PACKAGES"
        in describe_method("androidpublisher.edits.commit")["method"]["implementation_scope"]
    )


def test_reporting_through_actual_mcp_protocol():
    async def check():
        async with Client(create_server(Settings(packages=frozenset({PACKAGE})))) as client:
            contract = await client.call_tool("describe_api_method", {"method": QUERY})
            assert not contract.is_error
            with (
                patch("pubship.auth.Auth.token", return_value="synthetic"),
                patch("pubship.http.reporting_query", return_value={"rows": []}),
            ):
                result = await client.call_tool(
                    "read_reporting",
                    {
                        "method": QUERY,
                        "parameters": {"name": "apps/" + PACKAGE + "/crashRateMetricSet"},
                        "body": {"pageSize": 10},
                    },
                )
            assert not result.is_error
            assert result.structured_content["data"] == {"rows": []}

    asyncio.run(check())
