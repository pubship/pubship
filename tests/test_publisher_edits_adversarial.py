"""Independent, synthetic safety checks for reading existing Publisher edits."""

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
from pubship.errors import PlayError
from pubship.play import Play

PACKAGE = "com.example.app"
GET = "androidpublisher.edits.get"
TRACK = "androidpublisher.edits.tracks.get"


def parameters(**overrides):
    return {"packageName": PACKAGE, "editId": "existing-edit-42", **overrides}


@pytest.fixture
def reader():
    auth = Mock()
    auth.token.return_value = "synthetic-publisher-token"
    return Play(Settings(packages=frozenset({PACKAGE})), auth), auth


@pytest.mark.parametrize(
    "package", ["com.other.app", "com.example.app.evil", "com.example.app/../other", ""]
)
def test_edit_app_boundary_precedes_credentials_and_network(reader, package):
    play, auth = reader
    with patch("urllib.request.OpenerDirector.open") as provider:
        with pytest.raises(PlayError):
            play.read_edit(GET, parameters(packageName=package))
    auth.token.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("field", ["editId", "track", "language"])
@pytest.mark.parametrize(
    "value", ["..", ".", "../other", "x/y", "x%2Fy", "%252e%252e", "x\\y", "x\n", "\x00", "\ud800"]
)
def test_dangerous_edit_path_segments_are_rejected_before_credentials(reader, field, value):
    play, auth = reader
    method = "androidpublisher.edits.listings.get" if field == "language" else TRACK
    supplied = (
        parameters(language="en-US") if field == "language" else parameters(track="production")
    )
    supplied[field] = value
    with patch("urllib.request.OpenerDirector.open") as provider:
        with pytest.raises(PlayError):
            play.read_edit(method, supplied)
    auth.token.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize(
    "method",
    [
        "androidpublisher.edits.insert",
        "androidpublisher.edits.commit",
        "androidpublisher.edits.validate",
        "androidpublisher.edits.delete",
        "androidpublisher.edits.details.patch",
        "androidpublisher.edits.tracks.update",
        "androidpublisher.edits.bundles.upload",
        "androidpublisher.reviews.list",
        "playdeveloperreporting.apps.search",
    ],
)
def test_mutations_and_unselected_reads_cannot_reach_transport(reader, method):
    play, auth = reader
    with patch("urllib.request.OpenerDirector.open") as provider:
        with pytest.raises(PlayError):
            play.read_edit(method, parameters())
    auth.token.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize(
    "supplied",
    [
        None,
        [],
        {"packageName": PACKAGE},
        parameters(editId=True),
        parameters(editId=7),
        parameters(editId="x" * 1025),
        parameters(access_token="PRIVATE-ARGUMENT"),
        parameters(body={"status": "completed"}),
        parameters(editId=float("nan")),
    ],
)
def test_malformed_edit_parameters_are_redacted_and_do_not_authenticate(reader, supplied):
    play, auth = reader
    with patch("urllib.request.OpenerDirector.open") as provider:
        with pytest.raises(PlayError) as error:
            play.read_edit(GET, supplied)
    assert "PRIVATE-ARGUMENT" not in str(error.value)
    auth.token.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("status", [404, 409])
def test_missing_or_conflicting_edit_never_creates_retries_or_commits(reader, status):
    play, auth = reader
    failure = HTTPError(
        "https://androidpublisher.googleapis.com/synthetic",
        status,
        "PRIVATE-PROVIDER-DETAIL",
        {},
        io.BytesIO(b"PRIVATE-PROVIDER-BODY"),
    )
    with patch("urllib.request.OpenerDirector.open", side_effect=failure) as provider:
        with pytest.raises(PlayError) as error:
            play.read_edit(GET, parameters())
    provider.assert_called_once()
    sent = provider.call_args.args[0]
    assert sent.get_method() == "GET"
    assert sent.data is None
    assert sent.full_url.endswith(f"/applications/{PACKAGE}/edits/existing-edit-42")
    assert str(status) in str(error.value)
    assert "PRIVATE" not in str(error.value)
    auth.token.assert_called_once_with("publisher")


def test_track_snapshot_retains_draft_status_without_claiming_live_publication(reader):
    play, _ = reader
    data = {
        "track": "production",
        "releases": [
            {"name": "Synthetic draft", "versionCodes": ["200"], "status": "draft"},
            {
                "name": "Synthetic staged",
                "versionCodes": ["199"],
                "status": "inProgress",
                "userFraction": 0.1,
            },
        ],
    }
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(read=Mock(return_value=json.dumps(data).encode())))
    response.__exit__ = Mock(return_value=False)
    with patch("urllib.request.OpenerDirector.open", return_value=response) as provider:
        result = play.read_edit(TRACK, parameters(track="production"))
    assert result["data"] == data
    assert result["edit_id"] == "existing-edit-42"
    assert result["data_scope"] == "edit_snapshot"
    assert provider.call_args.args[0].get_method() == "GET"
    assert "/edits/existing-edit-42/tracks/production" in provider.call_args.args[0].full_url


def test_hosted_edit_reads_use_each_customers_grant_and_reject_other_apps(hosted):
    settings, _, client = hosted
    _, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-BOB")

    def provider_read(url, token):
        owner = {"PRIVATE-ALICE": "alice", "PRIVATE-BOB": "bob"}[token]
        assert f"/applications/com.example.{owner}/edits/edit-{owner}" in url
        return {"id": "edit-" + owner, "expiryTimeSeconds": "9999999999"}

    def read(owner, token):
        return call_tool(
            client,
            token,
            "read_publisher_edit",
            {
                "method": GET,
                "parameters": {"packageName": "com.example." + owner, "editId": "edit-" + owner},
            },
        )

    with patch("pubship.http.get", side_effect=provider_read) as provider:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(read, owner, tokens["access_token"])
                for owner, tokens in [("alice", alice), ("bob", bob)]
            ]
            responses = [future.result(timeout=10) for future in futures]
        for owner, response in zip(["alice", "bob"], responses, strict=True):
            assert not response.json()["result"].get("isError")
            assert response.json()["result"]["structuredContent"]["data"]["id"] == "edit-" + owner
            assert "PRIVATE" not in response.text
        assert read("bob", alice["access_token"]).json()["result"]["isError"]
    assert provider.call_count == 2


def test_hosted_reporting_permission_does_not_grant_publisher_edit_access(hosted):
    settings, _, client = hosted
    request = begin(client, settings, package=PACKAGE, services=("reporting",))
    code = callback(client, request, services=("publisher", "reporting"))
    tokens = exchange(client, settings, request, code).json()
    with patch("pubship.http.get") as provider:
        response = call_tool(
            client,
            tokens["access_token"],
            "read_publisher_edit",
            {"method": GET, "parameters": parameters()},
        )
    assert response.json()["result"]["isError"]
    assert "not authorized" in response.text
    provider.assert_not_called()


def test_tool_rejects_supplied_write_body_before_provider_access(hosted):
    settings, _, client = hosted
    _, tokens = connect_account(client, settings, PACKAGE, "PRIVATE-GOOGLE")
    with patch("pubship.http.get", return_value={"id": "existing-edit-42"}) as provider:
        response = call_tool(
            client,
            tokens["access_token"],
            "read_publisher_edit",
            {"method": GET, "parameters": parameters(), "body": {"status": "completed"}},
        )
    assert response.json()["result"]["isError"]
    provider.assert_not_called()


def test_real_stdio_exposes_edit_read_as_non_destructive_and_rejects_other_apps():
    async def exercise():
        transport = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pubship.server"],
            env={
                "GOOGLE_PLAY_PACKAGES": PACKAGE,
                "GOOGLE_PLAY_AUTH_MODE": "adc",
                "GOOGLE_APPLICATION_CREDENTIALS": "/synthetic-never-read",
            },
        )
        async with Client(transport) as client:
            tools = await client.list_tools()
            tool = next(tool for tool in tools.tools if tool.name == "read_publisher_edit")
            assert tool.annotations.read_only_hint
            assert not tool.annotations.destructive_hint
            assert "body" not in tool.input_schema["properties"]
            denied = await client.call_tool(
                "read_publisher_edit",
                {"method": GET, "parameters": parameters(packageName="com.example.other")},
            )
            assert denied.is_error
            assert "outside GOOGLE_PLAY_PACKAGES" in str(denied.content)

    asyncio.run(exercise())
