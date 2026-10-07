"""Independent synthetic safety checks for store-listing previews; no Google writes."""

import asyncio
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from test_hosted import begin, call_tool, callback, connect_account, exchange
from test_hosted import hosted as hosted

from pubship.config import Settings
from pubship.server import create_server

TOOL = "preview_store_listing_update"
PACKAGE = "com.example.app"


def parameters(**overrides):
    return {
        "packageName": PACKAGE,
        "editId": "existing-edit-42",
        "language": "en-US",
        **overrides,
    }


def invoke(arguments, factory):
    async def exercise():
        async with Client(create_server(service_factory=factory)) as client:
            return await client.call_tool(TOOL, arguments)

    return asyncio.run(exercise())


@pytest.fixture
def services():
    auth = Mock()
    auth.token.return_value = "synthetic-publisher-token"
    factory = Mock(return_value=(Settings(packages=frozenset({PACKAGE})), auth))
    return factory, auth


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"parameters": parameters()},
        {"parameters": None, "changes": {"title": "PRIVATE-INPUT"}},
        {"parameters": [], "changes": {"title": "PRIVATE-INPUT"}},
        {"parameters": parameters(), "changes": None},
        {"parameters": parameters(), "changes": []},
        {"parameters": parameters(), "changes": {}},
        {
            "parameters": {"packageName": PACKAGE, "editId": "existing-edit-42"},
            "changes": {"title": "x"},
        },
        {"parameters": parameters(editId="x" * 1025), "changes": {"title": "x"}},
        {"parameters": parameters(language="x" * 101), "changes": {"title": "x"}},
        {"parameters": parameters(), "changes": {"title": 3}},
        {"parameters": parameters(), "changes": {"title": False}},
        {"parameters": parameters(), "changes": {"title": None}},
        {"parameters": parameters(), "changes": {"title": ["PRIVATE-INPUT"]}},
        {"parameters": parameters(), "changes": {"title": {"secret": "PRIVATE-INPUT"}}},
        {"parameters": parameters(), "changes": {"language": "PRIVATE-INPUT"}},
        {"parameters": parameters(), "changes": {"video": "PRIVATE-INPUT"}},
        {"parameters": parameters(), "changes": {"title": "x" * 100_000}},
        {"parameters": parameters(), "changes": {"title": "\ud800"}},
        {"parameters": parameters(), "changes": {"fullDescription": "\udfff"}},
        {"parameters": parameters(access_token="PRIVATE-INPUT"), "changes": {"title": "x"}},
        {"parameters": parameters(body={"title": "PRIVATE-INPUT"}), "changes": {"title": "x"}},
        {"parameters": parameters(), "changes": {"title": "x"}, "body": "PRIVATE-INPUT"},
        {"parameters": parameters(), "changes": {"title": "x"}, "method": "PRIVATE-INPUT"},
        {"parameters": parameters(), "changes": {"title": "x"}, "execute": True},
    ],
)
def test_rejected_arguments_never_resolve_customer_services(arguments, caplog):
    factory = Mock(side_effect=AssertionError("Customer services must not be resolved"))
    with patch("urllib.request.OpenerDirector.open") as provider:
        result = invoke(arguments, factory)
    assert result.is_error
    assert "PRIVATE-INPUT" not in str(result.content) + caplog.text
    factory.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("field", ["packageName", "editId", "language"])
@pytest.mark.parametrize("value", ["", "..", "x/y", "x%2Fy", "x\\y", "\x00", "\ud800", 1, True])
def test_invalid_resource_paths_are_rejected_before_service_resolution(field, value):
    factory = Mock(side_effect=AssertionError("Unexpected service resolution"))
    result = invoke(
        {"parameters": parameters(**{field: value}), "changes": {"title": "x"}}, factory
    )
    assert result.is_error
    factory.assert_not_called()


def test_preview_uses_one_exact_get_and_preserves_supplied_body(services):
    factory, auth = services
    changes = {
        "title": "",
        "shortDescription": "  Café e\u0301 🌍  ",
        "fullDescription": "line 1\nline 2\r\n",
    }
    before = {"language": "en-US", "title": "", "fullDescription": changes["fullDescription"]}
    response = Mock()
    response.__enter__ = Mock(
        return_value=Mock(read=Mock(return_value=json.dumps(before).encode()))
    )
    response.__exit__ = Mock(return_value=False)
    with patch("urllib.request.OpenerDirector.open", return_value=response) as provider:
        result = invoke({"parameters": parameters(), "changes": changes}, factory)
    assert not result.is_error, result.content
    provider.assert_called_once()
    request = provider.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        + PACKAGE
        + "/edits/existing-edit-42/listings/en-US"
    )
    auth.token.assert_called_once_with("publisher")
    preview = result.structured_content
    assert preview["preview_only"] is True
    assert preview["executed"] is False
    assert preview["provider_validation_performed"] is False
    assert preview["stale_check_performed"] is False
    assert preview["proposed_request"]["method"] == "androidpublisher.edits.listings.patch"
    assert preview["proposed_request"]["parameters"] == parameters()
    assert preview["proposed_request"]["body"] == changes
    differences = {item["field"]: item for item in preview["changes"]}
    assert differences["title"] == {
        "field": "title",
        "before_present": True,
        "before": "",
        "after": "",
        "changed": False,
    }
    assert differences["shortDescription"] == {
        "field": "shortDescription",
        "before_present": False,
        "after": changes["shortDescription"],
        "changed": True,
    }
    assert differences["fullDescription"]["changed"] is False


@pytest.mark.parametrize(
    ("snapshot", "changes", "expected"),
    [
        (
            {"language": "en-US"},
            {"title": ""},
            {"field": "title", "before_present": False, "after": "", "changed": True},
        ),
        (
            {"language": "en-US", "title": "Café"},
            {"title": "Cafe\u0301"},
            {
                "field": "title",
                "before_present": True,
                "before": "Café",
                "after": "Cafe\u0301",
                "changed": True,
            },
        ),
    ],
)
def test_diff_preserves_missing_values_and_exact_unicode(services, snapshot, changes, expected):
    factory, _ = services
    with patch("pubship.http.get", return_value=snapshot):
        result = invoke({"parameters": parameters(), "changes": changes}, factory)
    assert not result.is_error, result.content
    assert result.structured_content["changes"] == [expected]
    assert result.structured_content["proposed_request"]["body"] == changes


@pytest.mark.parametrize(
    "snapshot",
    [
        [],
        None,
        "PRIVATE-PROVIDER",
        {},
        {"language": "fr-FR", "title": "PRIVATE-PROVIDER"},
        {"language": 1, "title": "PRIVATE-PROVIDER"},
        {"language": "en-US", "title": None},
        {"language": "en-US", "shortDescription": ["PRIVATE-PROVIDER"]},
        {"language": "en-US", "fullDescription": {"secret": "PRIVATE-PROVIDER"}},
    ],
)
def test_malformed_provider_listing_is_rejected_without_leaking_snapshot(
    services, snapshot, caplog
):
    factory, _ = services
    with patch("pubship.http.get", return_value=snapshot) as provider:
        result = invoke({"parameters": parameters(), "changes": {"title": "next"}}, factory)
    assert result.is_error
    assert "PRIVATE-PROVIDER" not in str(result.content) + caplog.text
    provider.assert_called_once()


@pytest.mark.parametrize("status", [403, 404, 409, 410, 429])
def test_provider_failure_never_creates_or_retries_an_edit(services, status, caplog):
    factory, _ = services
    failure = HTTPError(
        "https://androidpublisher.googleapis.com/synthetic",
        status,
        "PRIVATE-DETAIL",
        {},
        io.BytesIO(b"PRIVATE-BODY"),
    )
    with patch("urllib.request.OpenerDirector.open", side_effect=failure) as provider:
        result = invoke({"parameters": parameters(), "changes": {"title": "next"}}, factory)
    assert result.is_error
    assert str(status) in str(result.content)
    assert "PRIVATE" not in str(result.content) + caplog.text
    provider.assert_called_once()
    assert provider.call_args.args[0].get_method() == "GET"


def test_hosted_preview_isolates_customers_and_rejects_cross_account_apps(hosted, caplog):
    settings, _, client = hosted
    _, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-BOB")

    def provider_read(url, token):
        owner = {"PRIVATE-ALICE": "alice", "PRIVATE-BOB": "bob"}[token]
        assert f"/applications/com.example.{owner}/edits/edit-{owner}/listings/en-US" in url
        return {"language": "en-US", "title": owner}

    def preview(owner, token):
        return call_tool(
            client,
            token,
            TOOL,
            {
                "parameters": parameters(
                    packageName="com.example." + owner, editId="edit-" + owner
                ),
                "changes": {"title": "new-" + owner},
            },
        )

    with (
        patch("google.auth.default", side_effect=AssertionError("ADC forbidden")),
        patch("subprocess.run", side_effect=AssertionError("gcloud forbidden")),
        patch("pubship.http.get", side_effect=provider_read) as provider,
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(preview, owner, tokens["access_token"])
                for owner, tokens in [("alice", alice), ("bob", bob)]
            ]
            responses = [future.result(timeout=10) for future in futures]
        for owner, response in zip(["alice", "bob"], responses, strict=True):
            assert not response.json()["result"].get("isError"), response.text
            data = response.json()["result"]["structuredContent"]
            assert data["changes"][0]["before"] == owner
            assert data["proposed_request"]["body"] == {"title": "new-" + owner}
            assert "PRIVATE" not in response.text
        denied = preview("bob", alice["access_token"])
        assert denied.json()["result"]["isError"]
    assert provider.call_count == 2
    assert "PRIVATE" not in caplog.text


def test_hosted_reporting_consent_does_not_allow_publisher_preview(hosted):
    settings, _, client = hosted
    request = begin(client, settings, package=PACKAGE, services=("reporting",))
    code = callback(client, request, services=("publisher", "reporting"))
    tokens = exchange(client, settings, request, code).json()
    with patch("pubship.http.get") as provider:
        response = call_tool(
            client,
            tokens["access_token"],
            TOOL,
            {
                "parameters": parameters(),
                "changes": {"title": "next"},
            },
        )
    assert response.json()["result"]["isError"]
    assert "not authorized" in response.text
    provider.assert_not_called()


def test_real_stdio_preview_annotations_and_invalid_call_without_credentials(tmp_path):
    async def exercise():
        transport = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pubship.server"],
            env={
                "GOOGLE_PLAY_PACKAGES": PACKAGE,
                "GOOGLE_PLAY_AUTH_MODE": "adc",
                "GOOGLE_APPLICATION_CREDENTIALS": str(
                    tmp_path / "credentials-must-not-be-read.json"
                ),
            },
        )
        async with Client(transport) as client:
            tools = (await client.list_tools()).tools
            tool = next(tool for tool in tools if tool.name == TOOL)
            assert tool.annotations.read_only_hint
            assert not tool.annotations.destructive_hint
            assert tool.annotations.idempotent_hint
            assert tool.input_schema["additionalProperties"] is False
            assert set(tool.input_schema["properties"]) == {"parameters", "changes"}
            result = await client.call_tool(
                TOOL,
                {
                    "parameters": parameters(editId="../../PRIVATE-INPUT"),
                    "changes": {"title": "next"},
                },
            )
            assert result.is_error
            assert "Invalid Publisher resource identifier" in str(result.content)
            assert "PRIVATE-INPUT" not in str(result.content)
            assert "credentials-must-not-be-read" not in str(result.content)
            catalog = await client.call_tool(
                "describe_api_method",
                {
                    "method": "androidpublisher.edits.listings.patch",
                },
            )
            assert not catalog.is_error
            assert catalog.structured_content["method"]["status"] == "implemented"
            assert (
                "Local stdio opt-in" in catalog.structured_content["method"]["implementation_scope"]
            )
            assert "apply_store_listing_update" not in {
                tool.name for tool in (await client.list_tools()).tools
            }

    asyncio.run(exercise())
