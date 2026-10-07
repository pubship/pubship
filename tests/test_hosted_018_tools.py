"""New hosted tools through the official SDK and authenticated HTTP app."""

import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest
from mcp import Client
from test_hosted import hosted as hosted  # noqa: F401
from test_hosted_017_http import mcp_client
from test_hosted_018_registry import (
    EXPECTED_COMMERCE,
    EXPECTED_EDIT,
    PACKAGE,
    REPRESENTATIVES,
    WRITE_METHODS,
    commerce_case,
    setup_operation,
)
from test_hosted_consent import choose, finish, request_consent
from test_hosted_tools import hosted_tools as hosted_tools  # noqa: F401

from pubship import monetization_http
from pubship.errors import PlayError
from pubship.hosted_catalog import HOSTED_COMMERCE_QUERY_METHODS
from pubship.hosted_grants import READ_SCOPE
from pubship.hosted_tools import build_hosted_tools
from pubship.server import create_server

NEW_TOOLS = {
    "prepare_edit_write",
    "apply_edit_write",
    "query_monetization",
    "prepare_monetization_write",
    "apply_monetization_write",
}


def arguments(scenario):
    editing = scenario.kind == "edit_resource"
    return (
        "edit_write" if editing else "monetization_write",
        {
            "method": scenario.method,
            "parameters": scenario.parameters,
            "body": scenario.body,
            "acknowledge_effects" if editing else "acknowledge_live_effects": True,
        },
    )


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_all_44_new_write_methods_via_official_sdk(hosted_tools, monkeypatch, method):
    server, provider, registry, _ = hosted_tools
    scenario = setup_operation(monkeypatch, method)
    family, payload = arguments(scenario)
    provider.allowed = {READ_SCOPE: frozenset({PACKAGE}), scenario.capability: frozenset({PACKAGE})}

    async def run():
        async with Client(server) as client:
            prepared = await client.call_tool("prepare_" + family, payload)
            assert not prepared.is_error, prepared.content
            assert not scenario.writes
            operation = prepared.structured_content["operation_id"]
            result = await client.call_tool(
                "apply_" + family, {"operation_id": operation, "confirmation": operation}
            )
            assert not result.is_error, result.content
            assert result.structured_content["mutation_succeeded"]
            assert len(scenario.writes) == 1

    asyncio.run(run())
    assert not registry._records


@pytest.mark.parametrize("method", sorted(HOSTED_COMMERCE_QUERY_METHODS))
def test_all_three_queries_via_sdk_are_read_only_and_bounded(hosted_tools, monkeypatch, method):
    server, provider, registry, _ = hosted_tools
    parameters, body = commerce_case(method)
    query = Mock(return_value={"http_status": 200, "data": {"futureField": 1}})
    mutation = Mock(side_effect=AssertionError("Query attempted a mutation"))
    monkeypatch.setattr(monetization_http, "query", query)
    monkeypatch.setattr(monetization_http, "execute", mutation)
    provider.allowed = {
        READ_SCOPE: frozenset({PACKAGE}),
        "play:commerce-query": frozenset({PACKAGE}),
    }

    async def run():
        async with Client(server) as client:
            result = await client.call_tool(
                "query_monetization", {"method": method, "parameters": parameters, "body": body}
            )
            assert not result.is_error, result.content
            assert result.structured_content["data"] == {"futureField": 1}
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools["query_monetization"].annotations.read_only_hint
            assert tools["query_monetization"].annotations.idempotent_hint
            assert not tools["query_monetization"].annotations.destructive_hint

    asyncio.run(run())
    query.assert_called_once_with(method, parameters, body, "original-synthetic-token")
    mutation.assert_not_called()
    assert not registry._records


@pytest.mark.parametrize("missing", sorted(NEW_TOOLS))
def test_partial_new_tool_registration_cannot_advertise_full_catalog(missing):
    tools = build_hosted_tools(Mock(), Mock(), lambda: None)
    with pytest.raises(PlayError, match="complete hosted tool set"):
        create_server(
            service_factory=lambda: None,
            hosted_tools=[tool for tool in tools if tool.name != missing],
            hosted_catalog_context=lambda: None,
        )


@pytest.mark.parametrize(
    "name,payload",
    [
        ("prepare_edit_write", {"method": [], "parameters": {}}),
        (
            "prepare_edit_write",
            {
                "method": "androidpublisher.edits.details.patch",
                "parameters": {"packageName": PACKAGE},
                "acknowledge_effects": "true",
            },
        ),
        (
            "prepare_monetization_write",
            {
                "method": "androidpublisher.monetization.subscriptions.patch",
                "parameters": {"packageName": PACKAGE},
                "acknowledge_live_effects": 1,
            },
        ),
        (
            "prepare_monetization_write",
            {
                "method": "androidpublisher.inappproducts.delete",
                "parameters": {"packageName": PACKAGE},
                "_execution_authorization": "synthetic-private",
            },
        ),
        (
            "query_monetization",
            {
                "method": "androidpublisher.monetization.convertRegionPrices",
                "parameters": {"packageName": PACKAGE},
                "body": {},
                "local": True,
            },
        ),
        ("apply_edit_write", {"operation_id": 42, "confirmation": "42"}),
        (
            "apply_monetization_write",
            {
                "operation_id": "unreviewed",
                "confirmation": "unreviewed",
                "method": "androidpublisher.orders.refund",
            },
        ),
    ],
)
def test_new_wire_contracts_reject_coercion_and_internal_hook_injection(
    hosted_tools, name, payload
):
    server, provider, registry, _ = hosted_tools

    async def run():
        async with Client(server) as client:
            result = await client.call_tool(name, payload)
            assert result.is_error
            assert "synthetic-private" not in str(result.content)

    asyncio.run(run())
    provider.services.assert_not_called()
    assert not registry._records


@pytest.mark.parametrize("method", REPRESENTATIVES)
def test_actual_authenticated_http_new_write_routes_require_explicit_capability(
    hosted, monkeypatch, method
):
    settings, app, browser = hosted
    scenario = setup_operation(monkeypatch, method)
    capability = "play:edit-resources" if method in EXPECTED_EDIT else EXPECTED_COMMERCE[method]
    family, payload = arguments(scenario)
    connect, _, request = request_consent(browser, settings, [READ_SCOPE, capability])
    owner = finish(
        browser,
        settings,
        request,
        choose(
            browser,
            settings,
            connect,
            packages=PACKAGE,
            capabilities=[capability],
            fields={capability: PACKAGE},
        ),
    )
    connect, _, request = request_consent(browser, settings, [READ_SCOPE, capability])
    declined = finish(
        browser, settings, request, choose(browser, settings, connect, packages=PACKAGE)
    )

    async def run():
        async with mcp_client(app, settings, declined) as client:
            denied = await client.call_tool("prepare_" + family, payload)
            assert denied.is_error
            assert not scenario.reads and not scenario.writes
        async with mcp_client(app, settings, owner) as client:
            assert len((await client.list_tools()).tools) == 46
            prepared = await client.call_tool("prepare_" + family, payload)
            assert not prepared.is_error, prepared.content
            assert not scenario.writes
            operation = prepared.structured_content["operation_id"]
            result = await client.call_tool(
                "apply_" + family, {"operation_id": operation, "confirmation": operation}
            )
            assert not result.is_error, result.content
            assert result.structured_content["mutation_succeeded"]
            assert len(scenario.writes) == 1
            assert scenario.writes[0][-1] == "PRIVATE-GOOGLE-ALICE"

    browser.portal.call(run)


def test_actual_authenticated_http_query_route_uses_current_consent(hosted, monkeypatch):
    settings, app, browser = hosted
    capability = "play:commerce-query"
    method = "androidpublisher.monetization.convertRegionPrices"
    parameters, body = commerce_case(method)
    query = Mock(return_value={"http_status": 200, "data": {"convertedRegionPrices": {}}})
    monkeypatch.setattr(monetization_http, "query", query)
    connect, _, request = request_consent(browser, settings, [READ_SCOPE, capability])
    token = finish(
        browser,
        settings,
        request,
        choose(
            browser,
            settings,
            connect,
            packages=PACKAGE,
            capabilities=[capability],
            fields={capability: PACKAGE},
        ),
    )

    async def run():
        async with mcp_client(app, settings, token) as client:
            payload = {"method": method, "parameters": parameters, "body": body}
            result = await client.call_tool("query_monetization", payload)
            assert not result.is_error, result.content
            query.assert_called_once_with(method, parameters, body, "PRIVATE-GOOGLE-ALICE")
            query.reset_mock()
            denied_payload = deepcopy(payload)
            denied_payload["parameters"]["packageName"] = "com.example.ungranted"
            denied = await client.call_tool("query_monetization", denied_payload)
            assert denied.is_error
            query.assert_not_called()

    browser.portal.call(run)
