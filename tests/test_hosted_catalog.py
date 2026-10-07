"""Prevent full local coverage from overstating a hosted connection's authority."""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from mcp import Client

from pubship.catalog import list_methods
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.hosted_catalog import HOSTED_CAPABILITIES, annotate_catalog, execution_info
from pubship.hosted_grants import (
    ADMIN_SCOPES,
    APPSTORE_PAIR_SCOPES,
    ORDINARY_SCOPES,
    READ_SCOPE,
    V2_SCOPES,
    ConnectionAuthorization,
)
from pubship.hosted_tools import build_hosted_tools
from pubship.server import create_server

PACKAGE = "com.example.catalog"


def authorization(**changes):
    base = ConnectionAuthorization(
        "private-subject",
        "private-client",
        {scope: frozenset({PACKAGE}) for scope in ORDINARY_SCOPES},
        9999999999,
        V2_SCOPES,
        frozenset({"publisher", "reporting", "storage"}),
        True,
        appstore_targets={
            cap: {"com.example.store": frozenset({PACKAGE})} for cap in APPSTORE_PAIR_SCOPES
        },
        appstore_event_stores=frozenset({"com.example.store"}),
        authorization_schema_version=2,
        binary_transfers_enabled=True,
    )
    return replace(base, **changes)


def test_exact_hosted_inventory_separate_from_local_implementation():
    local = list_methods(limit=200)
    hosted = annotate_catalog(local, authorization())
    supported = [m for m in hosted["methods"] if m["execution"]["implemented"]]
    assert len(supported) == len(HOSTED_CAPABILITIES) == 170
    assert sum(m["execution"]["available_for_connection"] for m in supported) == 161
    assert all(
        m["execution"]["available_for_connection"]
        == (m["execution"]["required_capability"] not in ADMIN_SCOPES)
        for m in supported
    )
    assert all("execution" not in m for m in local["methods"])
    assert hosted["total"] == local["total"] == 172
    assert hosted["implementation_scope"] == "local"
    assert hosted["execution_mode"] == "hosted"
    purchase = next(m for m in hosted["methods"] if m["id"] == "androidpublisher.users.list")
    assert purchase["status"] == "implemented"
    assert purchase["execution"]["implemented"]
    assert not purchase["execution"]["available_for_connection"]
    assert "private-subject" not in str(hosted) and "private-client" not in str(hosted)


def test_read_scope_and_google_publisher_scope_do_not_imply_write_consent():
    info = authorization(capability_packages={READ_SCOPE: frozenset({PACKAGE})})
    listing = execution_info("androidpublisher.edits.listings.patch", info)
    assert listing["implemented"] and not listing["available_for_connection"]
    assert listing["authorized_packages"] == []
    assert execution_info("androidpublisher.reviews.list", info)["available_for_connection"]


def test_google_services_and_bucket_narrow_effective_availability():
    publisher = authorization(google_services=frozenset({"publisher"}))
    assert execution_info("androidpublisher.reviews.list", publisher)["available_for_connection"]
    for method in ("playdeveloperreporting.apps.search", "storage.objects.list"):
        assert not execution_info(method, publisher)["available_for_connection"]
    assert not execution_info("storage.objects.get", authorization(bucket_configured=False))[
        "available_for_connection"
    ]
    no_publisher = authorization(google_services=frozenset({"reporting"}))
    assert not execution_info("androidpublisher.edits.commit", no_publisher)[
        "available_for_connection"
    ]


def test_sdk_catalog_uses_current_authorization_and_preserves_local_contract():
    current = [authorization()]

    def forbidden_services():
        raise AssertionError("Catalog must not fetch Google credentials or data")

    hosted = create_server(
        service_factory=forbidden_services,
        hosted_catalog_context=lambda: current[0],
        hosted_tools=build_hosted_tools(Mock(), Mock(), lambda: None),
    )
    local = create_server(Settings(packages=frozenset({PACKAGE})))

    async def run():
        async with Client(hosted) as client:
            result = await client.call_tool("list_api_methods", {"query": "reviews.list"})
            assert not result.is_error
            assert result.structured_content["methods"][0]["execution"]["available_for_connection"]
            current[0] = authorization(google_services=frozenset({"reporting"}))
            result = await client.call_tool(
                "describe_api_method", {"method": "androidpublisher.reviews.list"}
            )
            assert not result.is_error
            assert not result.structured_content["method"]["execution"]["available_for_connection"]
            assert "input_schema" in result.structured_content
        async with Client(local) as client:
            result = await client.call_tool("list_api_methods", {"query": "reviews.list"})
            assert not result.is_error
            assert "execution_mode" not in result.structured_content
            assert "execution" not in result.structured_content["methods"][0]

    asyncio.run(run())


@pytest.mark.parametrize("configuration", ["empty", "partial", "missing_context"])
def test_incomplete_hosted_tool_catalog_configuration_fails_closed(configuration):
    tools = build_hosted_tools(Mock(), Mock(), lambda: None)
    context = authorization
    if configuration == "empty":
        tools = []
    elif configuration == "partial":
        tools = [tool for tool in tools if tool.name != "apply_publisher_edit_operation"]
    else:
        context = None
    with pytest.raises(PlayError, match="complete hosted tool set"):
        create_server(
            service_factory=lambda: None, hosted_tools=tools, hosted_catalog_context=context
        )
