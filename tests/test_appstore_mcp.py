"""SDK-client checks plus a real stdio child using synthetic provider IO."""

import asyncio
import json
import os
import sys
from unittest.mock import Mock

import pytest
from mcp import Client
from mcp.server import MCPServer
from test_appstore_contracts import APP, STORE, P

from pubship.appstore_contracts import APPSTORE_METHODS, CREATE
from pubship.appstore_tools import appstore_tools
from pubship.config import Settings
from pubship.server import create_server, safe_tool


@pytest.fixture
def grants(monkeypatch):
    for key in list(os.environ):
        if key.startswith("GOOGLE_PLAY_APPSTORE_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_STORES", STORE)
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_METHODS", ",".join(sorted(APPSTORE_METHODS)))
    return Settings(packages=frozenset({STORE, APP}))


def server(settings, auth):
    return MCPServer("App-store protocol fixture", tools=appstore_tools(settings, auth, safe_tool))


def test_mcp_strict_arguments_scope_confirmation_and_redaction(grants):
    auth = Mock()
    auth.token.side_effect = AssertionError("No auth expected")

    async def run():
        async with Client(server(grants, auth)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert tools["query_appstore"].annotations.read_only_hint
            assert tools["apply_appstore_operation"].annotations.destructive_hint
            for arguments in (
                {
                    "method": CREATE,
                    "parameters": P,
                    "body": {"packageName": "com.other.app"},
                    "acknowledge_live_effects": True,
                },
                {
                    "method": CREATE,
                    "parameters": P,
                    "body": {"packageName": APP},
                    "acknowledge_live_effects": "true",
                },
                {
                    "method": CREATE,
                    "parameters": P,
                    "body": {"packageName": APP},
                    "extra": "PRIVATE",
                },
            ):
                response = await client.call_tool("prepare_appstore_operation", arguments)
                assert response.is_error
                assert "PRIVATE" not in json.dumps(response.model_dump(mode="json"))

    asyncio.run(run())
    auth.token.assert_not_called()


def test_hosted_environment_does_not_expose_tools(grants):
    async def run():
        async with Client(create_server(service_factory=Mock())) as client:
            assert not any("appstore" in t.name for t in (await client.list_tools()).tools)

    asyncio.run(run())


def test_real_stdio_prepare_apply_and_replay(grants, tmp_path):
    child = tmp_path / "appstore_stdio.py"
    child.write_text("""from unittest.mock import Mock
from pubship import appstore_http
from pubship.appstore_tools import appstore_tools
from pubship.config import Settings
from pubship.server import safe_tool
from mcp.server import MCPServer
settings = Settings.from_env()
auth = Mock()
auth.token.return_value = "SYNTHETIC_PRIVATE_CREDENTIAL"
appstore_http.execute = Mock(return_value={})
server = MCPServer("App-store stdio fixture", tools=appstore_tools(settings, auth, safe_tool))
server.run()
""")

    async def run():
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = {**os.environ, "GOOGLE_PLAY_PACKAGES": STORE + "," + APP}
        params = StdioServerParameters(command=sys.executable, args=[str(child)], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert "prepare_appstore_operation" in {t.name for t in tools.tools}
                prepared = await client.call_tool(
                    "prepare_appstore_operation",
                    {
                        "method": CREATE,
                        "parameters": P,
                        "body": {"packageName": APP},
                        "acknowledge_live_effects": True,
                    },
                )
                assert not prepared.is_error, prepared.content
                operation = prepared.structured_content["operation_id"]
                applied = await client.call_tool(
                    "apply_appstore_operation",
                    {"operation_id": operation, "confirmation": operation},
                )
                assert not applied.is_error, applied.content
                assert applied.structured_content["mutation_succeeded"]
                assert not applied.structured_content["readback_available"]
                replay = await client.call_tool(
                    "apply_appstore_operation",
                    {"operation_id": operation, "confirmation": operation},
                )
                assert replay.is_error
                for response in (prepared, applied, replay):
                    assert "SYNTHETIC_PRIVATE_CREDENTIAL" not in json.dumps(
                        response.model_dump(mode="json")
                    )

    asyncio.run(run())


def test_mcp_all_eight_methods_use_expected_transport(grants, tmp_path, monkeypatch):

    from test_appstore_contracts import case
    from test_appstore_transport import Response

    from pubship.appstore_contracts import READ_METHODS

    (tmp_path / "media.bin").write_bytes(b"synthetic media")
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_UPLOAD_ROOT", str(tmp_path))
    auth = Mock()
    auth.token.return_value = "SYNTHETIC_CREDENTIAL"
    calls = []
    queue = []

    def send(request, **kwargs):
        calls.append(request)
        if hasattr(request.data, "read"):
            while request.data.read(4096):
                pass
        return Response(queue.pop(0))

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Mock(handlers=[], open=send))

    async def run():
        async with Client(server(grants, auth)) as client:
            for method in sorted(APPSTORE_METHODS):
                parameters, body, mime, response = case(method)
                queue.append(response)
                if method in READ_METHODS:
                    result = await client.call_tool(
                        "query_appstore", {"method": method, "parameters": parameters}
                    )
                    assert not result.is_error, result.content
                    assert result.structured_content["data"] == response
                else:
                    args = {
                        "method": method,
                        "parameters": parameters,
                        "body": body,
                        "acknowledge_live_effects": True,
                    }
                    if mime:
                        args.update(filename="media.bin", mime_type=mime)
                    prepared = await client.call_tool("prepare_appstore_operation", args)
                    assert not prepared.is_error, prepared.content
                    operation = prepared.structured_content["operation_id"]
                    result = await client.call_tool(
                        "apply_appstore_operation",
                        {"operation_id": operation, "confirmation": operation},
                    )
                    assert not result.is_error, result.content
                    assert result.structured_content["mutation_response"] == response
            assert not queue

    asyncio.run(run())
    assert len(calls) == 8
    assert [r.method for r in calls].count("GET") == 2
    assert [r.method for r in calls].count("POST") == 6
