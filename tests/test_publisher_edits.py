import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from jsonschema import Draft202012Validator
from mcp import Client

from pubship.config import Settings
from pubship.contracts import describe_method, methods
from pubship.coverage import PUBLISHER_EDIT_METHODS
from pubship.errors import PlayError
from pubship.play import Play
from pubship.server import create_server

PACKAGE = "com.example.app"
EDIT = "synthetic-edit_123"
PREFIX = "androidpublisher.edits."
CASES = {
    "get": ({}, "", {"id": EDIT, "expiryTimeSeconds": "1791216000"}),
    "details.get": ({}, "/details", {"defaultLanguage": "en-US"}),
    "listings.get": (
        {"language": "zh-Hant-TW"},
        "/listings/zh-Hant-TW",
        {"language": "zh-Hant-TW", "title": "Synthetic app", "fullDescription": "Untrusted text"},
    ),
    "listings.list": ({}, "/listings", {"listings": [{"language": "en-US", "title": "App"}]}),
    "images.list": (
        {"language": "en-US", "imageType": "phoneScreenshots"},
        "/listings/en-US/phoneScreenshots",
        {"images": [{"id": "synthetic-image", "url": "https://untrusted.example/image.png"}]},
    ),
    "tracks.get": (
        {"track": "wear:production"},
        "/tracks/wear%3Aproduction",
        {"track": "wear:production", "releases": [{"status": "draft", "versionCodes": ["123"]}]},
    ),
    "tracks.list": (
        {},
        "/tracks",
        {
            "tracks": [
                {"track": "production", "releases": [{"status": "inProgress", "userFraction": 0.1}]}
            ]
        },
    ),
    "countryavailability.get": (
        {"track": "automotive:beta"},
        "/countryAvailability/automotive%3Abeta",
        {"countries": [{"countryCode": "PT"}], "syncWithProduction": False},
    ),
    "bundles.list": ({}, "/bundles", {"bundles": [{"versionCode": 123, "sha256": "synthetic"}]}),
    "apks.list": ({}, "/apks", {"apks": [{"versionCode": 122, "binary": {"sha256": "synthetic"}}]}),
}


def params(extra=None):
    return {"packageName": PACKAGE, "editId": EDIT, **(extra or {})}


def response_context(data):
    response = Mock()
    response.read.return_value = json.dumps(data).encode()
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    return context


def test_edit_allowlist_matches_exact_pinned_get_contracts():
    def walk(node):
        yield from node.get("methods", {}).values()
        for resource in node.get("resources", {}).values():
            yield from walk(resource)

    snapshot = Path(__file__).resolve().parents[1] / "api/discovery/androidpublisher.json"
    pinned = {method["id"]: method for method in walk(json.loads(snapshot.read_text()))}
    assert PUBLISHER_EDIT_METHODS == {PREFIX + name for name in CASES}
    for method_id in PUBLISHER_EDIT_METHODS:
        source = pinned[method_id]
        assert source["httpMethod"] == "GET"
        assert "request" not in source
        assert all(p["location"] == "path" for p in source["parameters"].values())
        for key in ("httpMethod", "path", "parameters", "response", "scopes"):
            assert methods()[method_id][key] == source[key]
        assert describe_method(method_id)["method"]["tool"] == "read_publisher_edit"
        Draft202012Validator.check_schema(describe_method(method_id)["input_schema"])


@pytest.mark.parametrize("name", CASES)
def test_each_edit_read_uses_one_get_and_preserves_provider_snapshot(name):
    extra, suffix, data = CASES[name]
    auth = Mock()
    auth.token.return_value = "synthetic-access"
    context = response_context(data)
    with patch("urllib.request.OpenerDirector.open", return_value=context) as request:
        result = Play(Settings(packages=frozenset({PACKAGE})), auth).read_edit(
            PREFIX + name, params(extra)
        )
    auth.token.assert_called_once_with("publisher")
    request.assert_called_once()
    sent = request.call_args.args[0]
    assert sent.get_method() == "GET"
    assert sent.data is None
    assert sent.full_url == (
        f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PACKAGE}/edits/{EDIT}"
        + suffix
    )
    assert sent.get_header("Authorization") == "Bearer synthetic-access"
    assert result["data"] == data
    assert result["method"] == PREFIX + name
    assert result["package"] == PACKAGE
    assert result["edit_id"] == EDIT
    assert result["data_scope"] == "edit_snapshot"
    assert "not live publication status" in result["note"]
    assert "No edit was created or modified" in result["note"]
    assert datetime.fromisoformat(result["fetched_at"]).tzinfo is not None


@pytest.mark.parametrize("name", CASES)
def test_empty_edit_response_is_not_filled_in_or_retried(name):
    with patch("pubship.http.get", return_value={}) as get:
        result = Play(Settings(packages=frozenset({PACKAGE})), Mock()).read_edit(
            PREFIX + name, params(CASES[name][0])
        )
    assert result["data"] == {}
    get.assert_called_once()


@pytest.mark.parametrize("name", CASES)
def test_every_edit_method_requires_existing_edit_and_explicit_package(name):
    for missing in ("editId", "packageName"):
        parameters = params(CASES[name][0])
        del parameters[missing]
        auth = Mock()
        with patch("pubship.http.get") as get, pytest.raises(PlayError):
            Play(Settings(packages=frozenset({PACKAGE})), auth).read_edit(PREFIX + name, parameters)
        auth.token.assert_not_called()
        get.assert_not_called()


@pytest.mark.parametrize(
    "value", ["INVALID_PRIVATE", "icon/../../tracks", "icon%2ftracks", "appImageTypeUnspecified"]
)
def test_image_enum_and_path_errors_fail_before_auth(value):
    auth = Mock()
    with patch("pubship.http.get") as get, pytest.raises(PlayError) as error:
        Play(Settings(packages=frozenset({PACKAGE})), auth).read_edit(
            PREFIX + "images.list", params({"language": "en-US", "imageType": value})
        )
    assert "PRIVATE" not in str(error.value)
    auth.token.assert_not_called()
    get.assert_not_called()


@pytest.mark.parametrize("data", [None, [], "PRIVATE", 1])
def test_non_object_edit_response_fails_with_redacted_error(data):
    with patch("pubship.http.get", return_value=data), pytest.raises(PlayError) as error:
        Play(Settings(packages=frozenset({PACKAGE})), Mock()).read_edit(PREFIX + "get", params())
    assert "PRIVATE" not in str(error.value)
    assert "response shape" in str(error.value)


@pytest.mark.parametrize("track", ["wear:production", "automotive:beta", "android_xr:closed-test"])
def test_release_status_supports_encoded_form_factor_tracks(track):
    auth = Mock()
    data = {"releases": [{"releaseLifecycleState": "IN_REVIEW"}]}
    with patch("pubship.http.get", return_value=data) as get:
        result = Play(Settings(packages=frozenset({PACKAGE})), auth).releases(PACKAGE, track)
    assert "/tracks/" + track.replace(":", "%3A") + "/releases" in get.call_args.args[0]
    assert "%253A" not in get.call_args.args[0]
    assert result["track"] == track
    assert result["data"] == data


@pytest.mark.parametrize(
    "track", ["wear%3Aproduction", "../production", "wear:production/other", "x\n", ".", ".."]
)
def test_release_track_injection_fails_before_auth(track):
    auth = Mock()
    with patch("pubship.http.get") as get, pytest.raises(PlayError):
        Play(Settings(packages=frozenset({PACKAGE})), auth).releases(PACKAGE, track)
    auth.token.assert_not_called()
    get.assert_not_called()


def test_edit_tool_strict_schema_rejects_extra_arguments_without_exposing_values():
    async def exercise():
        async with Client(create_server(Settings(packages=frozenset({PACKAGE})))) as client:
            tools = await client.list_tools()
            tool = next(tool for tool in tools.tools if tool.name == "read_publisher_edit")
            assert tool.input_schema["additionalProperties"] is False
            assert set(tool.input_schema["properties"]) == {"method", "parameters"}
            for extra in ({"body": {"status": "PRIVATE"}}, {"unexpected": "PRIVATE"}):
                with (
                    patch("pubship.auth.Auth.token") as token,
                    patch("pubship.http.get") as get,
                ):
                    result = await client.call_tool(
                        "read_publisher_edit",
                        {"method": PREFIX + "get", "parameters": params(), **extra},
                    )
                assert result.is_error
                assert "PRIVATE" not in str(result.content)
                token.assert_not_called()
                get.assert_not_called()
            with (
                patch("pubship.auth.Auth.token", return_value="synthetic"),
                patch("pubship.http.get", return_value={"id": EDIT}),
            ):
                result = await client.call_tool(
                    "read_publisher_edit", {"method": PREFIX + "get", "parameters": params()}
                )
            assert not result.is_error
            assert result.structured_content["data_scope"] == "edit_snapshot"
            assert result.structured_content["data"] == {"id": EDIT}

    asyncio.run(exercise())
