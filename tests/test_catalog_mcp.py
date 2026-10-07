import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from pubship.catalog import IMPLEMENTED, catalog, list_methods
from pubship.config import Settings
from pubship.coverage import (
    ARTIFACT_METHODS,
    EDIT_WRITE_METHODS,
    EXTERNAL_TRANSACTION_METHODS,
    MONETIZATION_WRITE_METHODS,
    PRICE_MIGRATION_METHODS,
    PURCHASE_ACTION_METHODS,
    SHARING_METHODS,
    SUPPORT_METHODS,
)
from pubship.server import create_server

ROOT = Path(__file__).resolve().parents[1]


def walk(node):
    yield from node.get("methods", {}).values()
    for resource in node.get("resources", {}).values():
        yield from walk(resource)


def test_catalog_covers_every_official_play_method():
    data = catalog()
    for api in ["androidpublisher", "playdeveloperreporting"]:
        source = json.loads((ROOT / "api/discovery" / f"{api}.json").read_text())
        expected = {method["id"] for method in walk(source)}
        actual = {method["id"] for method in data["methods"] if method["api"] == api}
        assert actual == expected
    assert len({m["id"] for m in data["methods"]}) == len(data["methods"])
    assert {m["id"] for m in data["methods"] if m["status"] == "implemented"} == set(IMPLEMENTED)


def test_catalog_generated_files_have_no_drift():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_catalog.py"), "--check"], check=True
    )


def test_catalog_filters_and_pagination():
    rows = list_methods(api="androidpublisher", status="implemented", query="", limit=2)
    assert rows["total"] > 2
    assert len(rows["methods"]) == 2
    assert rows["next_offset"] == 2
    assert all(row["tool"] for row in rows["methods"])
    assert list_methods(status="coming_soon")["total"] == 0


def test_registered_tools_match_implemented_catalog(monkeypatch):
    async def check():
        async with Client(create_server(Settings())) as client:
            tools = await client.list_tools()
            tools = tools.tools if hasattr(tools, "tools") else tools
            assert {tool.name for tool in tools} == (
                set(IMPLEMENTED.values())
                - {
                    "apply_store_listing_update",
                    "apply_publisher_edit_operation",
                    "apply_track_update",
                    "read_sensitive_publisher",
                    "apply_edit_write",
                    "apply_artifact_upload",
                    "download_artifact",
                    "apply_monetization_write",
                    "apply_support_operation",
                    "apply_purchase_operation",
                    "apply_internal_sharing_upload",
                    "apply_signing_operation",
                    "read_account_access",
                    "apply_account_access",
                    "query_appstore",
                    "apply_appstore_operation",
                }
            ) | {
                "list_api_methods",
                "describe_api_method",
                "preview_store_listing_update",
                "preview_track_update",
            }
            assert all(
                tool.annotations.read_only_hint and not tool.annotations.destructive_hint
                for tool in tools
            )
            assert len(tools) == 13

        packages = frozenset({"com.example.app"})
        settings = Settings(
            packages=packages,
            write_packages=packages,
            edit_packages=packages,
            track_packages=packages,
            sensitive_read_packages=packages,
            edit_write_packages=packages,
            edit_write_methods=EDIT_WRITE_METHODS,
            artifact_packages=packages,
            artifact_methods=ARTIFACT_METHODS | SHARING_METHODS,
            support_packages=packages,
            support_methods=SUPPORT_METHODS,
            purchase_packages=packages,
            purchase_methods=PURCHASE_ACTION_METHODS,
            external_transaction_packages=packages,
            external_transaction_methods=EXTERNAL_TRANSACTION_METHODS,
            price_migration_packages=packages,
            price_migration_methods=PRICE_MIGRATION_METHODS,
            monetization_packages=packages,
            monetization_methods=MONETIZATION_WRITE_METHODS,
        )
        from pubship.account_access_config import METHODS as ACCOUNT_METHODS
        from pubship.appstore_contracts import APPSTORE_METHODS
        from pubship.signing import METHODS as SIGNING_METHODS

        for key, value in {
            "GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS": "12345",
            "GOOGLE_PLAY_ACCOUNT_ACCESS_METHODS": ",".join(ACCOUNT_METHODS),
            "GOOGLE_PLAY_SIGNING_APPS": "com.example.app",
            "GOOGLE_PLAY_SIGNING_METHODS": ",".join(SIGNING_METHODS),
            "GOOGLE_PLAY_SIGNING_KMS_VERSIONS": "projects/example-project/locations/global/keyRings/ring/cryptoKeys/key/cryptoKeyVersions/1",
            "GOOGLE_PLAY_APPSTORE_STORES": "com.example.app",
            "GOOGLE_PLAY_APPSTORE_PACKAGES": "com.example.app",
            "GOOGLE_PLAY_APPSTORE_METHODS": ",".join(APPSTORE_METHODS),
        }.items():
            monkeypatch.setenv(key, value)
        async with Client(create_server(settings)) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 41
            names = {tool.name for tool in tools}
            assert set(IMPLEMENTED.values()) <= names
            assert {"prepare_edit_write", "apply_edit_write"} <= names

    asyncio.run(check())


def test_edit_resource_writes_have_explicit_local_implementation_scope():
    rows = catalog()["methods"]
    assert len(rows) == 172
    assert sum(row["status"] == "implemented" for row in rows) == 170
    assert sum(row["status"] == "coming_soon" for row in rows) == 0
    for row in rows:
        if row["id"] in EDIT_WRITE_METHODS:
            assert row["tool"] == "apply_edit_write"
            assert "GOOGLE_PLAY_EDIT_WRITE_METHODS" in row["implementation_scope"]
            assert "mandatory readback" in row["implementation_scope"]


def test_real_stdio_round_trip_without_credentials():
    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pubship.server"],
            env={"GOOGLE_PLAY_PACKAGES": "com.example.app", "GOOGLE_PLAY_AUTH_MODE": "adc"},
        )
        async with Client(params) as client:
            result = await client.call_tool(
                "list_api_methods", {"status": "implemented", "limit": 2}
            )
            assert not result.is_error
            data = result.structured_content
            assert data["total"] > 2
            assert len(data["methods"]) == 2
            invalid = await client.call_tool("list_releases", {"package": "com.example.other"})
            assert invalid.is_error
            assert "outside GOOGLE_PLAY_PACKAGES" in str(invalid.content)

    asyncio.run(check())
