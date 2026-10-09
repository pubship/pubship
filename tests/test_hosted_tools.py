"""Official SDK checks for bounded hosted tools."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server import MCPServer
from test_hosted_edits import PACKAGE, PARAMETERS, Provider, principal
from test_hosted_edits import transport as transport  # noqa: F401
from test_publisher_reads_adversarial import CASES

from pubship.coverage import PUBLISHER_READ_METHODS
from pubship.hosted_edits import HostedEditRegistry
from pubship.hosted_tools import build_hosted_tools


@pytest.fixture
def hosted_tools():
    provider = Provider()
    current = [principal()]
    registry = HostedEditRegistry(provider)
    server = MCPServer(
        "Synthetic hosted tools", tools=build_hosted_tools(provider, registry, lambda: current[0])
    )
    yield server, provider, registry, current
    registry.close()


def test_hosted_tools_strict_schema_annotations_and_safe_capabilities(hosted_tools):
    server, provider, _, _ = hosted_tools

    async def run():
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == {
                "get_connection_capabilities",
                "read_account_access",
                "prepare_account_access",
                "apply_account_access",
                "prepare_signing_operation",
                "apply_signing_operation",
                "begin_upload_transfer",
                "get_transfer_status",
                "discard_transfer",
                "prepare_artifact_upload",
                "apply_artifact_upload",
                "download_artifact",
                "prepare_internal_sharing_upload",
                "apply_internal_sharing_upload",
                "query_appstore",
                "prepare_appstore_operation",
                "apply_appstore_operation",
                "read_publisher",
                "read_sensitive_publisher",
                "prepare_support_operation",
                "apply_support_operation",
                "prepare_purchase_operation",
                "apply_purchase_operation",
                "prepare_edit_write",
                "apply_edit_write",
                "query_monetization",
                "prepare_monetization_write",
                "apply_monetization_write",
                "prepare_store_listing_update",
                "apply_store_listing_update",
                "prepare_track_update",
                "apply_track_update",
                "prepare_publisher_edit_operation",
                "apply_publisher_edit_operation",
            }
            for name, tool in tools.items():
                assert tool.input_schema["additionalProperties"] is False
                assert not any(key.startswith("_") for key in tool.input_schema["properties"])
                assert tool.annotations.read_only_hint == (
                    not name.startswith("apply_")
                    and name
                    not in {
                        "begin_upload_transfer",
                        "discard_transfer",
                        "download_artifact",
                        "prepare_artifact_upload",
                        "prepare_internal_sharing_upload",
                        "prepare_appstore_operation",
                        "prepare_account_access",
                        "prepare_edit_write",
                        "prepare_monetization_write",
                        "prepare_publisher_edit_operation",
                        "prepare_purchase_operation",
                        "prepare_signing_operation",
                        "prepare_store_listing_update",
                        "prepare_support_operation",
                        "prepare_track_update",
                    }
                )
                assert tool.annotations.destructive_hint == (
                    name.startswith("apply_") or name == "discard_transfer"
                )
            result = await client.call_tool("get_connection_capabilities", {})
            assert not result.is_error
            data = result.structured_content
            assert data["capability_packages"]["play:edit-commit"] == [PACKAGE]
            assert data["preparations"]["lost_on_restart"]
            assert "subject" not in data and "client_id" not in data
            assert "synthetic-token" not in json.dumps(data)

    asyncio.run(run())
    provider.services.assert_not_called()


def test_every_new_tool_via_real_mcp_calls(hosted_tools, transport):
    server, provider, registry, current = hosted_tools

    async def run():
        async with Client(server) as client:
            reads = await client.call_tool(
                "read_publisher",
                {
                    "method": "androidpublisher.monetization.subscriptions.list",
                    "parameters": {"packageName": PACKAGE},
                },
            )
            assert not reads.is_error and not reads.structured_content["sensitive"]
            cases = [
                ("store_listing_update", {"parameters": PARAMETERS, "changes": {"title": "new"}}),
                (
                    "track_update",
                    {
                        "parameters": {
                            "packageName": PACKAGE,
                            "editId": "edit-1",
                            "track": "production",
                        },
                        "releases": [{"status": "draft", "versionCodes": ["1"]}],
                        "acknowledge_effects": True,
                    },
                ),
            ]
            for action in ("create", "validate", "discard", "commit"):
                params = {"packageName": PACKAGE}
                if action != "create":
                    params["editId"] = "edit-1"
                cases.append(
                    (
                        "publisher_edit_operation",
                        {"action": action, "parameters": params, "acknowledge_effects": True},
                    )
                )
            for family, arguments in cases:
                prepared = await client.call_tool("prepare_" + family, arguments)
                assert not prepared.is_error, prepared.content
                operation_id = prepared.structured_content["operation_id"]
                current[0] = principal("same-account-separate-connection")
                stolen = await client.call_tool(
                    "apply_" + family, {"operation_id": operation_id, "confirmation": operation_id}
                )
                assert stolen.is_error
                current[0] = principal()
                applied = await client.call_tool(
                    "apply_" + family, {"operation_id": operation_id, "confirmation": operation_id}
                )
                assert not applied.is_error, applied.content
                assert applied.structured_content["executed"]
                replay = await client.call_tool(
                    "apply_" + family, {"operation_id": operation_id, "confirmation": operation_id}
                )
                assert replay.is_error
            assert not registry._records

    asyncio.run(run())
    assert transport.patch.call_count == 1 and transport.put.call_count == 1
    assert transport.lifecycle.call_count == 4


def test_all_nineteen_metadata_methods_keep_fixed_routes(hosted_tools, transport):
    server, _, _, _ = hosted_tools
    covered = set()

    async def run():
        async with Client(server) as client:
            for suffix, (parameters, expected_path, _) in CASES.items():
                method = "androidpublisher." + suffix
                if method not in PUBLISHER_READ_METHODS:
                    continue
                result = await client.call_tool(
                    "read_publisher",
                    {"method": method, "parameters": {"packageName": PACKAGE, **parameters}},
                )
                assert not result.is_error, result.content
                assert expected_path in transport.reads.call_args.args[0]
                covered.add(method)

    asyncio.run(run())
    assert covered == PUBLISHER_READ_METHODS
    assert transport.reads.call_count == 19


@pytest.mark.parametrize(
    "name,arguments",
    [
        (
            "read_publisher",
            {
                "method": "androidpublisher.edits.testers.get",
                "parameters": {"packageName": PACKAGE, "editId": "edit-1", "track": "production"},
            },
        ),
        (
            "read_publisher",
            {
                "method": "androidpublisher.orders.get",
                "parameters": {"packageName": PACKAGE, "orderId": "private"},
            },
        ),
        (
            "read_publisher",
            {
                "method": "androidpublisher.monetization.subscriptions.list",
                "parameters": {"packageName": "com.other.app"},
            },
        ),
        (
            "prepare_store_listing_update",
            {
                "parameters": PARAMETERS,
                "changes": {"title": "new"},
                "_execution_authorization": "PRIVATE",
            },
        ),
        ("prepare_track_update", {"parameters": {}, "releases": [], "acknowledge_effects": "true"}),
        (
            "prepare_publisher_edit_operation",
            {"parameters": {"packageName": PACKAGE}, "action": "create", "acknowledge_effects": 1},
        ),
        ("apply_store_listing_update", {"operation_id": 123, "confirmation": "123"}),
    ],
)
def test_boundary_rejects_extra_coerced_sensitive_and_cross_package(
    hosted_tools, transport, name, arguments
):
    server, provider, registry, _ = hosted_tools

    async def run():
        async with Client(server) as client:
            result = await client.call_tool(name, arguments)
            assert result.is_error
            assert "PRIVATE" not in json.dumps(result.model_dump(mode="json"))

    asyncio.run(run())
    assert not registry._records
    provider.services.assert_not_called()
    transport.reads.assert_not_called()


def test_poisoned_local_environment_never_grants_hosted_authority(
    hosted_tools, transport, monkeypatch
):
    server, provider, _, current = hosted_tools
    for name in [
        "GOOGLE_PLAY_PACKAGES",
        "GOOGLE_PLAY_WRITE_PACKAGES",
        "GOOGLE_PLAY_EDIT_PACKAGES",
        "GOOGLE_PLAY_TRACK_PACKAGES",
        "GOOGLE_PLAY_SENSITIVE_READ_PACKAGES",
    ]:
        monkeypatch.setenv(name, PACKAGE)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/never/read/local-credentials")
    monkeypatch.setattr(
        "pubship.config.Settings.from_env", lambda: pytest.fail("local Settings read")
    )
    provider.allowed = {"play:read": frozenset({PACKAGE})}

    async def run():
        async with Client(server) as client:
            result = await client.call_tool(
                "prepare_store_listing_update",
                {"parameters": PARAMETERS, "changes": {"title": "new"}},
            )
            assert result.is_error
            current[0] = None
            result = await client.call_tool("get_connection_capabilities", {})
            assert result.is_error

    asyncio.run(run())
    provider.services.assert_not_called()


def test_real_stdio_hosted_registry_protocol(tmp_path):
    script = tmp_path / "hosted_fixture.py"
    tests_path = str(Path(__file__).resolve().parent)
    script.write_text(
        """import sys, time
sys.path.insert(0, """
        + repr(tests_path)
        + """)
from test_hosted_edits import Provider, principal
from pubship import http, publishing_http
from pubship.hosted_edits import HostedEditRegistry
from pubship.hosted_tools import build_hosted_tools
from mcp.server import MCPServer
provider = Provider()
registry = HostedEditRegistry(provider)
def get(url, token, **kwargs):
    return {"language":"en-US","title":"old"} if "/listings/" in url else {"id":"edit-1","expiryTimeSeconds":str(int(time.time())+3600)}
http.get = get
publishing_http.listing_patch = lambda url, token, body, **kwargs: {"language":"en-US", **body}
server = MCPServer("Synthetic stdio", tools=build_hosted_tools(provider,registry,principal))
try:
    server.run()
finally:
    registry.close()
"""
    )

    async def run():
        async with Client(
            StdioServerParameters(command=sys.executable, args=[str(script)])
        ) as client:
            assert len((await client.list_tools()).tools) == 34
            capabilities = await client.call_tool("get_connection_capabilities", {})
            assert not capabilities.is_error
            result = await client.call_tool(
                "prepare_store_listing_update",
                {"parameters": PARAMETERS, "changes": {"title": "new"}},
            )
            assert not result.is_error, result.content
            operation_id = result.structured_content["operation_id"]
            applied = await client.call_tool(
                "apply_store_listing_update",
                {"operation_id": operation_id, "confirmation": operation_id},
            )
            assert not applied.is_error and applied.structured_content["executed"]

    asyncio.run(run())


def test_metadata_access_expiring_during_google_refresh_denies_dispatch(hosted_tools, transport):
    server, provider, _, current = hosted_tools

    def expire(service):
        current[0].expires = 0
        return "synthetic-token"

    provider.auth.token.side_effect = expire

    async def run():
        async with Client(server) as client:
            result = await client.call_tool(
                "read_publisher",
                {
                    "method": "androidpublisher.monetization.subscriptions.list",
                    "parameters": {"packageName": PACKAGE},
                },
            )
            assert result.is_error

    asyncio.run(run())
    transport.reads.assert_not_called()
