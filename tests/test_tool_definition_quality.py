"""Real SDK discovery regressions, using only isolated synthetic configuration.

Contract fingerprints were captured before definition enrichment on 2026-10-09
with mcp==2.3.0. They retain names, types, defaults, requiredness, constraints,
output schemas and annotations; only description strings are excluded. The reviewed
stateful preparation and hosted download readOnlyHint corrections are applied
to the baseline explicitly.
These checks measure definition coverage, not an external TDQS score.
"""

import asyncio
import hashlib
import json
import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from mcp import Client

from pubship.account_access_config import METHODS as ACCOUNT_METHODS
from pubship.appstore_contracts import APPSTORE_METHODS
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
from pubship.hosted import create_hosted_app
from pubship.hosted_config import HostedSettings
from pubship.server import create_server
from pubship.signing import METHODS as SIGNING_METHODS

HOSTED_LOCAL_EFFECTS = {
    "download_artifact",
    "prepare_artifact_upload",
    "prepare_internal_sharing_upload",
    "prepare_appstore_operation",
}

STATEFUL_PREPARATIONS = {
    "prepare_account_access",
    "prepare_appstore_operation",
    "prepare_artifact_upload",
    "prepare_edit_write",
    "prepare_internal_sharing_upload",
    "prepare_monetization_write",
    "prepare_publisher_edit_operation",
    "prepare_purchase_operation",
    "prepare_signing_operation",
    "prepare_store_listing_update",
    "prepare_support_operation",
    "prepare_track_update",
}

# Captured from actual tools/list, not recomputed from the implementation under test.
BASELINE_CONTRACTS = {
    "local-default": {
        "read_publisher": "aec1fc7c48f912a5ce1c1c4092dd6186b9e71a7f08f2fd294ba71e1bc69bf3a1",
        "read_publisher_edit": "36e933eceaae8ea9878812387b700ebfc53ba6d836d5509a716d841f83e9cc6f",
        "preview_store_listing_update": "4ff62e487fafb3f20b2cfda443351189c80dcc0847aa6d3c4679c54315f82a11",
        "preview_track_update": "36324289e05887a96447997da1626d36345711c29ac02545cab5202a108ec5e7",
        "query_monetization": "a97a44183b40b589edcd59d126cea56ee2237d9611d28db34e84b86ac61ac3ad",
        "list_api_methods": "ae0bd8f8ff369d612cf8fa96df95984951a1c32c425adafa48bbe1c2ae388181",  # gitleaks:allow public contract SHA256, not a credential
        "describe_api_method": "c4eba4c6f27c1670e8d09c2929c40a9e71dd65596e3f239b174d462018fa606a",  # gitleaks:allow public contract SHA256, not a credential
        "read_reporting": "81099f4867d4efc37af86a7df88495d9768a3cddfb02ac84b286e09f04157ee0",
        "list_releases": "ffb5d8ff96b208708bac69e2970031b65732cead1bf1240deb4f2e061fe3f5a2",
        "list_reviews": "bf73b6890839f5adbd610382b86b793cd60908941faf525d61dc0d240305732d",
        "get_review": "dbeed3e8b45bd29c6cf36407b7c76dd4be2abdb56077648e290c299c18dacad4",
        "list_report_files": "1093be0d9d9e8ca6447cdba776de7c6c0a68b3d88b5aaed2e63765bc8de4030d",
        "read_report": "5dc42b9dad234bdf79f42839f96992f0f9408f3c3f6e855806c0e8a8ef3388a8",
    },
    "local-all-opt-ins": {
        "read_publisher": "aec1fc7c48f912a5ce1c1c4092dd6186b9e71a7f08f2fd294ba71e1bc69bf3a1",
        "read_sensitive_publisher": "a8b6662f0517afa45b32f912e9061dc4b10b193585736f319d84b1446a547b06",
        "read_publisher_edit": "36e933eceaae8ea9878812387b700ebfc53ba6d836d5509a716d841f83e9cc6f",
        "preview_store_listing_update": "4ff62e487fafb3f20b2cfda443351189c80dcc0847aa6d3c4679c54315f82a11",
        "preview_track_update": "36324289e05887a96447997da1626d36345711c29ac02545cab5202a108ec5e7",
        "prepare_store_listing_update": "22c1a98820443837d7a50bdd1852a777714eaad8413312835d77d2b4cc9dc8f1",
        "apply_store_listing_update": "a49e0ffe9c22204b775f621b0c0edbfacb3e4a2b6ede0cb03987599750510171",
        "prepare_publisher_edit_operation": "fdf412f65bad04f7c0851a31d57205047709c4ae91b44107058b6a44cd1a5290",
        "apply_publisher_edit_operation": "ced5e062009a3ac652ab8d4a2aab523e56b1bf9a404b81cdf9ec1883d6cf8058",
        "prepare_track_update": "5023f457490deea642b5eeb906c8c8548777c4bd0864aed9a0eebf83558462b7",
        "apply_track_update": "d6fe319d906eab2ea0c3f4f923ba1e94b912875d833923fe1461e0ae08e47c1d",
        "prepare_edit_write": "d45bb4b38c3b964957d1fcf751ef623df0deec86d7873038a6d4f4ca9a2dc5bc",
        "apply_edit_write": "f5cee369e99e5b3a30d6ef0e3920f5b71088f4162114cbca2df08bf7db79acfc",
        "prepare_artifact_upload": "f8914d767d9fd8cf360d63dcdd1446a815cb0f316cfa5ad696128b0f77c2fdaa",
        "apply_artifact_upload": "00db7514e5147f14772db07b7dffbb6eb024315da627033e261064979c660f07",
        "download_artifact": "8c4403be1acdb665a9cff8d9833d2cdce48e5f209991775ab96c63a025afe861",
        "query_monetization": "a97a44183b40b589edcd59d126cea56ee2237d9611d28db34e84b86ac61ac3ad",
        "prepare_monetization_write": "40325fe71bcec541177dd08f517ff77b5fb83383985326161d874e9aba7fe637",
        "apply_monetization_write": "f1d0002e67d6a5c1b56070878426f3d8008bf6e3002862577149c2570bfc1c72",
        "prepare_support_operation": "b4f929d998af03cbdbe54f0ed7341cb7a0358de008b591322ad83de5210681da",
        "apply_support_operation": "da2990172111fbf3f371a8d0d679aae8188bcaf39122dc35c048182964c96dfb",
        "prepare_purchase_operation": "c9c7f5bf352bf32eaa1e91c941e5d8a10165a9cf2510c8dadd6eccd12301657a",
        "apply_purchase_operation": "611f4737e353514cc2ebc04eb37c96deebe71185eed83511e9185cc428b0dd77",
        "prepare_internal_sharing_upload": "521a26f45df2cdc8ff25d8fbf93125076a56ed746977c20796e21afc86bda388",
        "apply_internal_sharing_upload": "7bd98a2f2b8945fb6e9667f6db20e9fe153398d67e9ed280f6e6f777a77fb930",
        "prepare_signing_operation": "1cf13bf4a5bc881fa67c02fe934bf90397e6d0fd657a7893fd5b9e39d1b03e79",
        "apply_signing_operation": "b492b5dd4bbc6c775d9de5d05c817cfee33bab20e34bd77de6026d728469c1d4",
        "read_account_access": "fb247b089b966b38f2eedab33c0dbd3dba85d3724769b2d2a5c1381a8105fd43",  # gitleaks:allow public contract SHA256, not a credential
        "prepare_account_access": "8eb3f223ff3ae4ac56e49bdf3b6852cf1f30de72f2fdb81ce2c07f8f2314276e",  # gitleaks:allow public contract SHA256, not a credential
        "apply_account_access": "ebdb1a5684edbb3a010cae19aed58d9fedbc49894282de7d03b2aaaef0360541",  # gitleaks:allow public contract SHA256, not a credential
        "query_appstore": "ccb42bf716370fd7e863e78c9bdf392baaa39d08be4e731a3c139d527652519f",
        "prepare_appstore_operation": "e27a7e9eea0db5ab7f21d913ac8978d355109bab568e91f4a0627f091fc88caf",
        "apply_appstore_operation": "5fb2f09267f6d4c9ddcf0755c4ed2f6e6ecf4e9b75cf67ebaff2b85e4432bb58",
        "list_api_methods": "ae0bd8f8ff369d612cf8fa96df95984951a1c32c425adafa48bbe1c2ae388181",  # gitleaks:allow public contract SHA256, not a credential
        "describe_api_method": "c4eba4c6f27c1670e8d09c2929c40a9e71dd65596e3f239b174d462018fa606a",  # gitleaks:allow public contract SHA256, not a credential
        "read_reporting": "81099f4867d4efc37af86a7df88495d9768a3cddfb02ac84b286e09f04157ee0",
        "list_releases": "ffb5d8ff96b208708bac69e2970031b65732cead1bf1240deb4f2e061fe3f5a2",
        "list_reviews": "bf73b6890839f5adbd610382b86b793cd60908941faf525d61dc0d240305732d",
        "get_review": "dbeed3e8b45bd29c6cf36407b7c76dd4be2abdb56077648e290c299c18dacad4",
        "list_report_files": "1093be0d9d9e8ca6447cdba776de7c6c0a68b3d88b5aaed2e63765bc8de4030d",
        "read_report": "5dc42b9dad234bdf79f42839f96992f0f9408f3c3f6e855806c0e8a8ef3388a8",
    },
    "self-host": {
        "read_publisher_edit": "36e933eceaae8ea9878812387b700ebfc53ba6d836d5509a716d841f83e9cc6f",
        "preview_store_listing_update": "4ff62e487fafb3f20b2cfda443351189c80dcc0847aa6d3c4679c54315f82a11",
        "preview_track_update": "36324289e05887a96447997da1626d36345711c29ac02545cab5202a108ec5e7",
        "get_connection_capabilities": "026102274d5636ab0f32f64efe894dcd963b1918f1d5b6af4e48cd66bf438720",
        "read_account_access": "fb247b089b966b38f2eedab33c0dbd3dba85d3724769b2d2a5c1381a8105fd43",  # gitleaks:allow public contract SHA256, not a credential
        "prepare_account_access": "8eb3f223ff3ae4ac56e49bdf3b6852cf1f30de72f2fdb81ce2c07f8f2314276e",  # gitleaks:allow public contract SHA256, not a credential
        "apply_account_access": "ebdb1a5684edbb3a010cae19aed58d9fedbc49894282de7d03b2aaaef0360541",  # gitleaks:allow public contract SHA256, not a credential
        "prepare_signing_operation": "1cf13bf4a5bc881fa67c02fe934bf90397e6d0fd657a7893fd5b9e39d1b03e79",
        "apply_signing_operation": "b492b5dd4bbc6c775d9de5d05c817cfee33bab20e34bd77de6026d728469c1d4",
        "begin_upload_transfer": "692b7aab6a7adc2ae9aef0d2de352d932288ec41ead9e2143eaf686dd0a966af",
        "get_transfer_status": "eb49b068afecd216480aa99db73da0ff04a15777cb708d33b86cc090dee28623",
        "discard_transfer": "034d3247ea095c2bccd893340a0ec089135d4c40e43439fb7047ed9b7e721fb3",
        "prepare_artifact_upload": "604bd58b94fb99fc8009a2fc207dbc979b8b6a5ffdf02bb9b11abd13172e0b43",
        "apply_artifact_upload": "00db7514e5147f14772db07b7dffbb6eb024315da627033e261064979c660f07",
        "download_artifact": "8c4403be1acdb665a9cff8d9833d2cdce48e5f209991775ab96c63a025afe861",
        "prepare_internal_sharing_upload": "655234fc5cef8086981e258d7bc4f968a5247af7ffd2002e9962942c21c4d64d",
        "apply_internal_sharing_upload": "7bd98a2f2b8945fb6e9667f6db20e9fe153398d67e9ed280f6e6f777a77fb930",
        "query_appstore": "ccb42bf716370fd7e863e78c9bdf392baaa39d08be4e731a3c139d527652519f",
        "prepare_appstore_operation": "e9534ed39072b29773994168cb394ef16615952d2b3d199b37ccf51c5c616ce0",
        "apply_appstore_operation": "5fb2f09267f6d4c9ddcf0755c4ed2f6e6ecf4e9b75cf67ebaff2b85e4432bb58",
        "read_publisher": "aec1fc7c48f912a5ce1c1c4092dd6186b9e71a7f08f2fd294ba71e1bc69bf3a1",
        "read_sensitive_publisher": "a8b6662f0517afa45b32f912e9061dc4b10b193585736f319d84b1446a547b06",
        "prepare_support_operation": "b4f929d998af03cbdbe54f0ed7341cb7a0358de008b591322ad83de5210681da",
        "apply_support_operation": "da2990172111fbf3f371a8d0d679aae8188bcaf39122dc35c048182964c96dfb",
        "prepare_purchase_operation": "c9c7f5bf352bf32eaa1e91c941e5d8a10165a9cf2510c8dadd6eccd12301657a",
        "apply_purchase_operation": "611f4737e353514cc2ebc04eb37c96deebe71185eed83511e9185cc428b0dd77",
        "prepare_edit_write": "d45bb4b38c3b964957d1fcf751ef623df0deec86d7873038a6d4f4ca9a2dc5bc",
        "apply_edit_write": "f5cee369e99e5b3a30d6ef0e3920f5b71088f4162114cbca2df08bf7db79acfc",
        "query_monetization": "a97a44183b40b589edcd59d126cea56ee2237d9611d28db34e84b86ac61ac3ad",
        "prepare_monetization_write": "40325fe71bcec541177dd08f517ff77b5fb83383985326161d874e9aba7fe637",
        "apply_monetization_write": "f1d0002e67d6a5c1b56070878426f3d8008bf6e3002862577149c2570bfc1c72",
        "prepare_store_listing_update": "22c1a98820443837d7a50bdd1852a777714eaad8413312835d77d2b4cc9dc8f1",
        "apply_store_listing_update": "a49e0ffe9c22204b775f621b0c0edbfacb3e4a2b6ede0cb03987599750510171",
        "prepare_track_update": "5023f457490deea642b5eeb906c8c8548777c4bd0864aed9a0eebf83558462b7",
        "apply_track_update": "d6fe319d906eab2ea0c3f4f923ba1e94b912875d833923fe1461e0ae08e47c1d",
        "prepare_publisher_edit_operation": "fdf412f65bad04f7c0851a31d57205047709c4ae91b44107058b6a44cd1a5290",
        "apply_publisher_edit_operation": "ced5e062009a3ac652ab8d4a2aab523e56b1bf9a404b81cdf9ec1883d6cf8058",
        "list_api_methods": "ae0bd8f8ff369d612cf8fa96df95984951a1c32c425adafa48bbe1c2ae388181",  # gitleaks:allow public contract SHA256, not a credential
        "describe_api_method": "c4eba4c6f27c1670e8d09c2929c40a9e71dd65596e3f239b174d462018fa606a",  # gitleaks:allow public contract SHA256, not a credential
        "read_reporting": "81099f4867d4efc37af86a7df88495d9768a3cddfb02ac84b286e09f04157ee0",
        "list_releases": "ffb5d8ff96b208708bac69e2970031b65732cead1bf1240deb4f2e061fe3f5a2",
        "list_reviews": "bf73b6890839f5adbd610382b86b793cd60908941faf525d61dc0d240305732d",
        "get_review": "dbeed3e8b45bd29c6cf36407b7c76dd4be2abdb56077648e290c299c18dacad4",
        "list_report_files": "1093be0d9d9e8ca6447cdba776de7c6c0a68b3d88b5aaed2e63765bc8de4030d",
        "read_report": "5dc42b9dad234bdf79f42839f96992f0f9408f3c3f6e855806c0e8a8ef3388a8",
        "disconnect_google_account": "69936c080d983dc2deb829f9a3f537f1de1a48a689415ffa1fe89f0e3296c6c2",
    },
}


def _without_descriptions(value):
    if isinstance(value, dict):
        return {
            key: _without_descriptions(item) for key, item in value.items() if key != "description"
        }
    if isinstance(value, list):
        return [_without_descriptions(item) for item in value]
    return value


def _fingerprint(tool):
    encoded = json.dumps(
        _without_descriptions(tool), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _all_opt_ins(monkeypatch):
    packages = frozenset({"com.example.app"})
    for key, value in {
        "GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS": "12345",
        "GOOGLE_PLAY_ACCOUNT_ACCESS_METHODS": ",".join(ACCOUNT_METHODS),
        "GOOGLE_PLAY_SIGNING_APPS": "com.example.app",
        "GOOGLE_PLAY_SIGNING_METHODS": ",".join(SIGNING_METHODS),
        "GOOGLE_PLAY_SIGNING_KMS_VERSIONS": (
            "projects/example-project/locations/global/keyRings/ring/cryptoKeys/key/cryptoKeyVersions/1"
        ),
        "GOOGLE_PLAY_APPSTORE_STORES": "com.example.app",
        "GOOGLE_PLAY_APPSTORE_PACKAGES": "com.example.app",
        "GOOGLE_PLAY_APPSTORE_METHODS": ",".join(APPSTORE_METHODS),
    }.items():
        monkeypatch.setenv(key, value)
    return Settings(
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


@pytest.fixture(params=tuple(BASELINE_CONTRACTS))
def exported_tools(request, monkeypatch, tmp_path):
    # Discovery must work without credentials, real operator settings or networking.
    for name in tuple(os.environ):
        if (
            name.startswith(("GOOGLE_PLAY_", "PUBSHIP_SERVER_"))
            or name == "GOOGLE_APPLICATION_CREDENTIALS"
        ):
            monkeypatch.delenv(name)
    profile = request.param
    app = None
    with (
        patch("pubship.auth.Auth.token", side_effect=AssertionError("Credential access")),
        patch("google.auth.default", side_effect=AssertionError("ADC access")),
        patch("socket.socket.connect", side_effect=AssertionError("Network access")),
    ):
        if profile == "self-host":
            captured = []

            def capture(**kwargs):
                server = create_server(**kwargs)
                captured.append(server)
                return server

            settings = HostedSettings(
                "https://mcp.example.test",
                "synthetic-client",
                "synthetic-secret",
                tmp_path / "vault.db",
                Fernet.generate_key(),
                allowed_emails=frozenset({"owner@example.test"}),
            )
            with patch("pubship.hosted.create_server", side_effect=capture):
                app = create_hosted_app(settings)
            server = captured[0]
        else:
            settings = _all_opt_ins(monkeypatch) if profile == "local-all-opt-ins" else Settings()
            server = create_server(settings)

        async def export():
            async with Client(server) as client:
                result = await client.list_tools()
                return [tool.model_dump(by_alias=True, exclude_none=True) for tool in result.tools]

        try:
            yield profile, asyncio.run(export())
        finally:
            if app is not None:
                app.state.hosted_registry.close()
                app.state.hosted_transfers.close()
                app.state.vault.close()


def test_sdk_discovery_preserves_behavioral_contracts(exported_tools):
    profile, tools = exported_tools
    actual = {tool["name"]: _fingerprint(tool) for tool in tools}
    assert len(tools) == len(actual), "Duplicate tool registration"
    assert list(actual) == list(BASELINE_CONTRACTS[profile]), "Tool registration/order changed"
    for name, fingerprint in actual.items():
        assert fingerprint == BASELINE_CONTRACTS[profile][name], (
            f"{profile}/{name}: non-description contract changed"
        )


def _input_properties(schema, path="inputSchema"):
    if isinstance(schema, dict):
        for name, value in schema.get("properties", {}).items():
            yield f"{path}.properties.{name}", value
        for key, value in schema.items():
            yield from _input_properties(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from _input_properties(value, f"{path}[{index}]")


def test_every_discovered_input_has_a_description(exported_tools):
    profile, tools = exported_tools
    for tool in tools:
        assert tool.get("description", "").strip(), f"{profile}/{tool['name']}"
        for path, property_schema in _input_properties(tool["inputSchema"]):
            description = property_schema.get("description", "").strip()
            assert description, f"{profile}/{tool['name']}/{path} lacks a description"
            assert description.lower() not in {"todo", "tbd", "description", "parameter"}


def test_reviewed_side_effect_annotations_are_exact(exported_tools):
    profile, tools = exported_tools
    by_name = {tool["name"]: tool for tool in tools}
    expected = STATEFUL_PREPARATIONS | ({"download_artifact"} if profile == "self-host" else set())
    for name in expected & by_name.keys():
        assert by_name[name]["annotations"] == {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }


@pytest.mark.parametrize("hosted", [False, True])
def test_enrichment_is_idempotent_and_does_not_mutate_argument_models(hosted):
    from copy import deepcopy

    from mcp.server.mcpserver.tools import Tool

    from pubship.tool_metadata import enrich_tool

    def list_reviews(package: str, limit: int = 10) -> dict[str, str | int]:
        """Read recent written reviews."""
        return {"package": package, "limit": limit}

    tool = Tool.from_function(list_reviews, structured_output=True)
    tool.parameters["properties"]["package"]["description"] = "Existing caller-specific guidance."
    original_parameters = tool.parameters
    original_schema = deepcopy(original_parameters)
    original_argument_model = tool.fn_metadata.arg_model.model_json_schema()
    original_function = tool.fn
    original_metadata = tool.fn_metadata
    original_output = deepcopy(tool.output_schema)
    enrich_tool(tool, hosted=hosted)
    once = (tool.description, deepcopy(tool.parameters))
    enrich_tool(tool, hosted=hosted)

    assert (tool.description, tool.parameters) == once
    assert tool.parameters is not original_parameters
    assert original_parameters == original_schema
    assert tool.parameters["properties"]["package"]["description"] == (
        "Existing caller-specific guidance."
    )
    assert _without_descriptions(tool.parameters) == _without_descriptions(original_schema)
    assert tool.fn_metadata.arg_model.model_json_schema() == original_argument_model
    assert tool.fn is original_function
    assert tool.fn_metadata is original_metadata
    assert tool.output_schema == original_output
    assert tool.fn("com.example.app") == {"package": "com.example.app", "limit": 10}


def test_unknown_extension_metadata_is_untouched():
    from copy import deepcopy

    from mcp.server.mcpserver.tools import Tool

    from pubship.tool_metadata import enrich_tool

    def extension(value: int = 3) -> dict[str, int]:
        """Caller-owned description with  intentional spacing."""
        return {"value": value}

    tool = Tool.from_function(extension, structured_output=True)
    original_schema = tool.parameters
    original = deepcopy(tool.model_dump(exclude={"fn_metadata"}))
    original_metadata = tool.fn_metadata
    for hosted in (False, True):
        enrich_tool(tool, hosted=hosted)
        assert tool.parameters is original_schema
        assert tool.model_dump(exclude={"fn_metadata"}) == original
        assert tool.fn_metadata is original_metadata


def test_mode_guidance_distinguishes_local_files_from_hosted_handles(exported_tools):
    profile, tools = exported_tools
    by_name = {tool["name"]: tool for tool in tools}
    if profile == "self-host":
        assert "get_connection_capabilities" in by_name["list_api_methods"]["description"]
        assert "content path" in by_name["download_artifact"]["description"]
        for name in HOSTED_LOCAL_EFFECTS - {"download_artifact"}:
            description = by_name[name]["inputSchema"]["properties"]["upload_handle"]["description"]
            assert "READY" in description and "connection" in description
        for name, tool in by_name.items():
            if name.startswith("apply_"):
                description = tool["inputSchema"]["properties"]["operation_id"]["description"]
                assert "connection" in description and "restart" in description
    elif profile == "local-all-opt-ins":
        assert "GOOGLE_PLAY_ARTIFACT_DOWNLOAD_ROOT" in by_name["download_artifact"]["description"]
        assert (
            "GOOGLE_PLAY_ARTIFACT_UPLOAD_ROOT"
            in (
                by_name["prepare_artifact_upload"]["inputSchema"]["properties"]["filename"][
                    "description"
                ]
            )
        )


def test_registration_preserves_caller_extension_precedence(monkeypatch):
    from mcp.server.mcpserver.tools import Tool

    for name in tuple(os.environ):
        if name.startswith("GOOGLE_PLAY_") or name == "GOOGLE_APPLICATION_CREDENTIALS":
            monkeypatch.delenv(name)

    def list_reviews(value: int = 7) -> dict[str, int]:
        """Caller-owned replacement, with its own input and output contract."""
        return {"value": value}

    replacement = Tool.from_function(list_reviews, structured_output=True)
    original_description = replacement.description
    original_parameters = replacement.parameters
    with (
        patch("pubship.auth.Auth.token", side_effect=AssertionError("Credential access")),
        patch("google.auth.default", side_effect=AssertionError("ADC access")),
        patch("socket.socket.connect", side_effect=AssertionError("Network access")),
    ):
        server = create_server(Settings(), tools=[replacement])

        async def run():
            async with Client(server) as client:
                listed = (await client.list_tools()).tools
                matching = [tool for tool in listed if tool.name == "list_reviews"]
                assert len(matching) == 1
                assert matching[0].description == original_description
                assert matching[0].input_schema == original_parameters
                result = await client.call_tool("list_reviews", {})
                assert not result.is_error
                assert result.structured_content == {"value": 7}

        asyncio.run(run())
    assert replacement.parameters is original_parameters
