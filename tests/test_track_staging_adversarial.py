"""Independent track-staging acceptance using synthetic providers and MCP clients."""

import asyncio
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from test_hosted import begin, call_tool, callback, connect_account, exchange
from test_hosted import hosted as hosted

from pubship.config import Settings
from pubship.edit_lifecycle import EditLifecycle
from pubship.errors import PlayError
from pubship.server import create_server
from pubship.track_staging import TrackStaging
from pubship.track_staging_http import execute

APP = "com.example.app"
PARAMS = {"packageName": APP, "editId": "owned-edit", "track": "wear:production"}
RELEASES = [{"status": "draft", "versionCodes": ["9223372036854775807"]}]


def fixture_service():
    auth = Mock()
    auth.token.return_value = "PRIVATE-ORIGINAL-TOKEN"
    service = TrackStaging(
        Settings(packages=frozenset({APP}), track_packages=frozenset({APP})), auth, local=True
    )
    edit = {"id": "owned-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    track = {"track": PARAMS["track"], "releases": [{"status": "completed", "versionCodes": ["1"]}]}
    return service, auth, edit, track


@pytest.mark.parametrize(
    "arguments",
    [
        {"parameters": PARAMS, "releases": RELEASES, "execute": True},
        {"parameters": {**PARAMS, "access_token": "PRIVATE"}, "releases": RELEASES},
        {"parameters": {**PARAMS, "track": "wear%3Aproduction"}, "releases": RELEASES},
        {"parameters": {**PARAMS, "track": "production/../internal"}, "releases": RELEASES},
        {"parameters": {**PARAMS, "editId": "id:commit"}, "releases": RELEASES},
        {"parameters": PARAMS, "releases": None},
        {"parameters": PARAMS, "releases": {}},
        {"parameters": PARAMS, "releases": [{"status": "draft", "versionCodes": [True]}]},
        {"parameters": PARAMS, "releases": [{"status": "draft", "versionCodes": [123]}]},
        {
            "parameters": PARAMS,
            "releases": [{"status": "draft", "versionCodes": ["1"], "inAppUpdatePriority": True}],
        },
        {
            "parameters": PARAMS,
            "releases": [{"status": "inProgress", "versionCodes": ["1"], "userFraction": True}],
        },
        {
            "parameters": PARAMS,
            "releases": [{"status": "draft", "versionCodes": ["1"], "name": "\ud800"}],
        },
        {
            "parameters": PARAMS,
            "releases": [
                {
                    "status": "draft",
                    "versionCodes": ["1"],
                    "releaseNotes": [{"language": "en-US", "text": "\ud800"}],
                }
            ],
        },
        {
            "parameters": PARAMS,
            "releases": [
                {
                    "status": "draft",
                    "versionCodes": ["1"],
                    "releaseNotes": [{"language": "en-US", "text": 12}],
                }
            ],
        },
        {
            "parameters": PARAMS,
            "releases": [{"status": "draft", "versionCodes": ["1"], "unknown": "PRIVATE"}],
        },
        {
            "parameters": PARAMS,
            "releases": [
                {
                    "status": "inProgress",
                    "versionCodes": ["1"],
                    "userFraction": 0.5,
                    "countryTargeting": {"includeRestOfWorld": "false"},
                }
            ],
        },
    ],
)
def test_preview_malformed_input_never_resolves_customer_credentials(arguments, caplog):
    factory = Mock(side_effect=AssertionError("Invalid input must not resolve services"))

    async def exercise():
        async with Client(create_server(service_factory=factory)) as client:
            assert "preview_track_update" in {
                tool.name for tool in (await client.list_tools()).tools
            }
            result = await client.call_tool("preview_track_update", arguments)
            assert result.is_error
            assert "PRIVATE" not in str(result.content)

    with patch("urllib.request.OpenerDirector.open") as provider:
        asyncio.run(exercise())
    factory.assert_not_called()
    provider.assert_not_called()
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("fraction", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_input_never_reaches_auth_or_provider(fraction):
    service, auth, _, _ = fixture_service()
    with patch("pubship.http.get") as read:
        with pytest.raises(PlayError):
            service.prepare(
                PARAMS,
                [{"status": "inProgress", "versionCodes": ["1"], "userFraction": fraction}],
                True,
            )
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("ack", [False, None, 0, 1, "true"])
def test_prepare_requires_exact_boolean_acknowledgement_on_mcp_wire(ack):
    settings = Settings(packages=frozenset({APP}), track_packages=frozenset({APP}))

    async def exercise():
        async with Client(create_server(settings)) as client:
            assert "prepare_track_update" in {
                tool.name for tool in (await client.list_tools()).tools
            }
            result = await client.call_tool(
                "prepare_track_update",
                {"parameters": PARAMS, "releases": RELEASES, "acknowledge_effects": ack},
            )
            assert result.is_error

    with (
        patch("pubship.auth.Auth.token") as token,
        patch("pubship.http.get") as read,
    ):
        asyncio.run(exercise())
    token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("mode", ["default", "listing", "lifecycle", "hosted"])
def test_track_write_capability_is_separate_and_hosted_cannot_inherit_env(mode, monkeypatch):
    for name in ["PACKAGES", "WRITE_PACKAGES", "EDIT_PACKAGES", "TRACK_PACKAGES"]:
        monkeypatch.setenv("GOOGLE_PLAY_" + name, APP)
    factory = Mock(side_effect=AssertionError("Discovery must not resolve credentials"))
    settings = Settings(
        packages=frozenset({APP}),
        write_packages=frozenset({APP}) if mode in {"listing", "lifecycle"} else frozenset(),
        edit_packages=frozenset({APP}) if mode == "lifecycle" else frozenset(),
    )

    async def exercise():
        server = (
            create_server(service_factory=factory) if mode == "hosted" else create_server(settings)
        )
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "preview_track_update" in names
            assert "prepare_track_update" not in names
            assert "apply_track_update" not in names
            denied = await client.call_tool(
                "apply_track_update", {"operation_id": "x", "confirmation": "x"}
            )
            assert denied.is_error

    asyncio.run(exercise())
    factory.assert_not_called()


def test_hosted_track_preview_uses_each_grant_and_rejects_cross_account_apps(hosted, caplog):
    settings, _, client = hosted
    _, alice = connect_account(client, settings, "com.example.alice", "PRIVATE-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "PRIVATE-BOB")

    def provider_read(url, token):
        owner = {"PRIVATE-ALICE": "alice", "PRIVATE-BOB": "bob"}[token]
        assert (
            f"/applications/com.example.{owner}/edits/edit-{owner}/tracks/wear%3Aproduction" in url
        )
        return {"track": PARAMS["track"], "releases": [{"name": owner, "status": "draft"}]}

    def preview(owner, token):
        return call_tool(
            client,
            token,
            "preview_track_update",
            {
                "parameters": {
                    **PARAMS,
                    "packageName": "com.example." + owner,
                    "editId": "edit-" + owner,
                },
                "releases": RELEASES,
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
            result = response.json()["result"]
            assert not result.get("isError"), response.text
            assert result["structuredContent"]["before"]["releases"][0]["name"] == owner
            assert "PRIVATE" not in response.text
        assert preview("bob", alice["access_token"]).json()["result"]["isError"]
    assert provider.call_count == 2
    assert "PRIVATE" not in caplog.text


def test_hosted_reporting_consent_does_not_grant_track_access(hosted):
    settings, _, client = hosted
    request = begin(client, settings, package=APP, services=("reporting",))
    code = callback(client, request, services=("publisher", "reporting"))
    token = exchange(client, settings, request, code).json()["access_token"]
    with patch("pubship.http.get") as provider:
        response = call_tool(
            client, token, "preview_track_update", {"parameters": PARAMS, "releases": RELEASES}
        )
    assert response.json()["result"]["isError"]
    provider.assert_not_called()


def test_original_identity_and_deep_release_body_survive_mutated_inputs_and_output():
    service, auth, edit, track = fixture_service()
    params, releases = dict(PARAMS), deepcopy(RELEASES)
    releases[0]["releaseNotes"] = [{"language": "en-US", "text": "  Café e\u0301 🌍\n"}]
    expected = deepcopy(releases)
    normalized = {
        "track": PARAMS["track"],
        "releases": [{"status": "completed", "name": "provider-fallback"}],
    }
    with patch("pubship.http.get", side_effect=[edit, track, edit, track]) as read:
        prepared = service.prepare(params, releases, True)
        params["track"] = "internal"
        releases[0]["versionCodes"].append("2")
        prepared["proposed_request"]["body"]["releases"][0]["releaseNotes"][0]["text"] = (
            "NOT-REVIEWED"
        )
        prepared["before"]["releases"][0]["name"] = "NOT-BASELINE"
        auth.token.return_value = "PRIVATE-DIFFERENT-IDENTITY"
        with patch("pubship.track_staging_http.execute", return_value=normalized) as write:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_called_once_with(PARAMS, expected, "PRIVATE-ORIGINAL-TOKEN")
    auth.token.assert_called_once_with("publisher")
    assert all(call.args[1] == "PRIVATE-ORIGINAL-TOKEN" for call in read.call_args_list)
    assert result["data"] == normalized
    assert result["executed"] and not result["committed"] and not result["publication_verified"]
    assert "PRIVATE" not in json.dumps(prepared) + json.dumps(result)


@pytest.mark.parametrize(
    "change", ["extra_field", "release_note", "absent_releases", "edit_metadata"]
)
def test_full_baseline_drift_consumes_preparation_without_put(change):
    service, _, edit, track = fixture_service()
    current, current_edit = deepcopy(track), deepcopy(edit)
    if change == "extra_field":
        current["futureProviderField"] = "changed"
    elif change == "release_note":
        current["releases"][0]["releaseNotes"] = [{"language": "en-US", "text": "someone else"}]
    elif change == "absent_releases":
        del current["releases"]
    else:
        current_edit["expiryTimeSeconds"] = str(int(edit["expiryTimeSeconds"]) + 1)
    with patch("pubship.http.get", side_effect=[edit, track, current_edit, current]):
        prepared = service.prepare(PARAMS, RELEASES, True)
        with patch("pubship.track_staging_http.execute") as write:
            with pytest.raises(PlayError):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_not_called()


def test_track_apply_blocks_lifecycle_and_same_id_replay_while_in_flight():
    service, auth, edit, track = fixture_service()
    lifecycle = EditLifecycle(
        Settings(
            packages=frozenset({APP}),
            write_packages=frozenset({APP}),
            edit_packages=frozenset({APP}),
        ),
        auth,
        local=True,
    )
    with patch("pubship.http.get", side_effect=[edit, track, edit]):
        prepared = service.prepare(PARAMS, RELEASES, True)
        commit = lifecycle.prepare("commit", {"packageName": APP, "editId": "owned-edit"}, True)
    entered, release = threading.Event(), threading.Event()

    def slow_put(*args):
        entered.set()
        assert release.wait(5)
        return {"track": PARAMS["track"], "releases": RELEASES}

    with (
        patch("pubship.http.get", side_effect=[edit, track]) as read,
        patch("pubship.track_staging_http.execute", side_effect=slow_put) as put,
        patch("pubship.edit_lifecycle_http.execute") as dispatch_commit,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(service.apply, prepared["operation_id"], prepared["operation_id"])
        try:
            assert entered.wait(2)
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="in progress"):
                lifecycle.apply(commit["operation_id"], commit["operation_id"])
            dispatch_commit.assert_not_called()
            assert read.call_count == 2
        finally:
            release.set()
        assert pending.result(timeout=2)["executed"]
        put.assert_called_once()
    with pytest.raises(PlayError, match="consumed"):
        lifecycle.apply(commit["operation_id"], commit["operation_id"])


@pytest.mark.parametrize("code", [401, 403, 409, 429, 500, 503])
def test_put_http_errors_never_retry_or_leak_provider_details(code):
    error = HTTPError(
        "https://androidpublisher.googleapis.com/",
        code,
        "PRIVATE-PROVIDER",
        {},
        io.BytesIO(b"PRIVATE-DETAIL"),
    )
    with patch("urllib.request.OpenerDirector.open", side_effect=error) as send:
        with pytest.raises(PlayError, match="outcome unknown") as caught:
            execute(PARAMS, RELEASES, "PRIVATE-TOKEN")
    send.assert_called_once()
    assert send.call_args.args[0].get_method() == "PUT"
    assert "PRIVATE" not in str(caught.value)
    assert "No writes were attempted" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [None, [], {}, {"track": "internal"}, {"track": "wear:production", "releases": "bad"}],
)
def test_invalid_post_put_response_consumes_operation_and_reports_unknown(response):
    service, _, edit, track = fixture_service()
    with patch("pubship.http.get", side_effect=[edit, track, edit, track]):
        prepared = service.prepare(PARAMS, RELEASES, True)
        with patch("pubship.track_staging_http.execute", return_value=response) as write:
            with pytest.raises(PlayError, match="outcome unknown"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_called_once()


def test_real_stdio_preview_stage_clear_and_replay_without_google_credentials(tmp_path):
    launcher = tmp_path / "track_stdio.py"
    launcher.write_text("""
import io, json, time
from unittest.mock import patch
from pubship.auth import Auth
from pubship.server import main
edit={"id":"owned-edit","expiryTimeSeconds":str(int(time.time())+3600)}
track={"track":"wear:production","releases":[{"status":"completed","versionCodes":["1"]}]}
base="https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.example.app/edits/owned-edit"
def send(self, request, timeout):
    assert request.get_header("Authorization")=="Bearer synthetic-track-stdio"
    assert timeout==15
    if request.full_url==base and request.get_method()=="GET":
        assert request.data is None
        data=edit
    elif request.full_url==base+"/tracks/wear%3Aproduction":
        if request.get_method()=="PUT":
            body=json.loads(request.data)
            assert set(body)=={"track","releases"} and body["track"]=="wear:production"
            track.clear()
            track.update(body)
        else:
            assert request.get_method()=="GET" and request.data is None
        data=track
    else:
        raise AssertionError("Unexpected provider route or mutation")
    return io.BytesIO(json.dumps(data).encode())
with patch.object(Auth,"token",return_value="synthetic-track-stdio"),patch("urllib.request.OpenerDirector.open",send):
    main()
""")

    async def exercise():
        connection = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher)],
            env={
                "GOOGLE_PLAY_PACKAGES": APP,
                "GOOGLE_PLAY_TRACK_PACKAGES": APP,
                "GOOGLE_PLAY_WRITE_PACKAGES": "",
                "GOOGLE_PLAY_EDIT_PACKAGES": "",
                "GOOGLE_PLAY_AUTH_MODE": "adc",
            },
        )
        async with Client(connection) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools["preview_track_update"].annotations.read_only_hint
            assert "prepare_store_listing_update" not in tools
            assert "prepare_publisher_edit_operation" not in tools
            apply = tools["apply_track_update"]
            assert not apply.annotations.read_only_hint and apply.annotations.destructive_hint
            assert not apply.annotations.idempotent_hint
            for releases in [RELEASES, []]:
                preview = await client.call_tool(
                    "preview_track_update", {"parameters": PARAMS, "releases": releases}
                )
                assert not preview.is_error, preview.content
                assert preview.structured_content["executed"] is False
                prepared = await client.call_tool(
                    "prepare_track_update",
                    {"parameters": PARAMS, "releases": releases, "acknowledge_effects": True},
                )
                assert not prepared.is_error, prepared.content
                identifier = prepared.structured_content["operation_id"]
                forged = await client.call_tool(
                    apply.name,
                    {
                        "operation_id": identifier,
                        "confirmation": identifier,
                        "releases": [{"name": "PRIVATE"}],
                    },
                )
                assert forged.is_error and "PRIVATE" not in str(forged.content)
                result = await client.call_tool(
                    apply.name, {"operation_id": identifier, "confirmation": identifier}
                )
                assert not result.is_error, result.content
                assert result.structured_content["data"] == {
                    "track": PARAMS["track"],
                    "releases": releases,
                }
                assert result.structured_content["put_attempted"] is True
                assert not result.structured_content["committed"]
                assert not result.structured_content["publication_verified"]
                replay = await client.call_tool(
                    apply.name, {"operation_id": identifier, "confirmation": identifier}
                )
                assert replay.is_error

    asyncio.run(exercise())


def test_read_access_to_app_does_not_imply_track_write_access():
    auth = Mock()
    service = TrackStaging(
        Settings(packages=frozenset({APP, "com.other.app"}), track_packages=frozenset({APP})),
        auth,
        local=True,
    )
    with patch("pubship.http.get") as read:
        with pytest.raises(PlayError, match="TRACK_PACKAGES"):
            service.prepare({**PARAMS, "packageName": "com.other.app"}, RELEASES, True)
    auth.token.assert_not_called()
    read.assert_not_called()


def test_track_and_lifecycle_preparation_ids_never_cross_authorize():
    service, auth, edit, track = fixture_service()
    lifecycle = EditLifecycle(
        Settings(
            packages=frozenset({APP}),
            write_packages=frozenset({APP}),
            edit_packages=frozenset({APP}),
        ),
        auth,
        local=True,
    )
    with patch("pubship.http.get", side_effect=[edit, track]):
        prepared = service.prepare(PARAMS, RELEASES, True)
    create = lifecycle.prepare("create", {"packageName": APP}, True)
    with (
        patch("pubship.http.get") as read,
        patch("pubship.track_staging_http.execute") as put,
        patch("pubship.edit_lifecycle_http.execute") as mutation,
    ):
        with pytest.raises(PlayError):
            lifecycle.apply(prepared["operation_id"], prepared["operation_id"])
        with pytest.raises(PlayError):
            service.apply(create["operation_id"], create["operation_id"])
    read.assert_not_called()
    put.assert_not_called()
    mutation.assert_not_called()


@pytest.mark.parametrize(
    "malformed_release",
    [
        {},
        {"status": "draft", "versionCodes": ["not-an-int"]},
        {"status": "draft", "versionCodes": [str(2**63)]},
        {"status": "draft", "versionCodes": [True]},
        {"status": "draft", "versionCodes": [1.5]},
        {"status": "draft", "versionCodes": []},
        {"status": "draft", "versionCodes": ["-1"]},
        {"status": "inProgress", "userFraction": True},
        {"status": "draft", "inAppUpdatePriority": False},
        {"status": "draft", "releaseNotes": [{"language": "en-US", "text": 1}]},
        {"status": "inProgress", "countryTargeting": {"includeRestOfWorld": "false"}},
    ],
)
def test_one_malformed_release_among_valid_put_results_is_unknown_and_consumed(malformed_release):
    service, _, edit, track = fixture_service()
    response = {
        "track": PARAMS["track"],
        "releases": [
            {"status": "completed", "versionCodes": ["1"]},
            malformed_release,
        ],
    }
    with patch("pubship.http.get", side_effect=[edit, track, edit, track]):
        prepared = service.prepare(PARAMS, RELEASES, True)
        # Exercise the real mutation decoder/validator, not a mocked successful
        # execute() result. One valid release cannot conceal a malformed second.
        with patch(
            "urllib.request.OpenerDirector.open",
            return_value=io.BytesIO(json.dumps(response).encode()),
        ) as send:
            with pytest.raises(PlayError, match="outcome unknown"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
        send.assert_called_once()
        assert send.call_args.args[0].get_method() == "PUT"


def test_success_preserves_unknown_fields_future_states_and_omitted_optional_identity():
    service, _, edit, track = fixture_service()
    response = {
        "track": PARAMS["track"],
        "futureTrackMetadata": {"preserve": [None, "exact"]},
        "releases": [
            {
                "status": "futureProviderState",
                "name": "Provider fallback without optional version codes",
                "futureReleaseMetadata": {"flag": True},
                "releaseNotes": [
                    {"language": "en-US", "text": "  Café e\u0301\n", "futureNote": "keep"}
                ],
            }
        ],
    }
    with patch("pubship.http.get", side_effect=[edit, track, edit, track]):
        prepared = service.prepare(PARAMS, RELEASES, True)
        with patch(
            "urllib.request.OpenerDirector.open",
            return_value=io.BytesIO(json.dumps(response).encode()),
        ) as send:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
    send.assert_called_once()
    assert result["data"] == response
    assert result["data"]["releases"] != RELEASES
    assert "versionCodes" not in result["data"]["releases"][0]
    assert result["executed"] and not result["publication_verified"]


def test_omitted_read_releases_remain_absent_but_cannot_prove_nonempty_put_success():
    service, _, edit, _ = fixture_service()
    snapshot = {"track": PARAMS["track"], "futureTrackMetadata": "keep"}
    with patch("pubship.http.get", side_effect=[edit, snapshot, edit, snapshot]):
        prepared = service.prepare(PARAMS, RELEASES, True)
        assert prepared["before"] == snapshot
        assert "releases" not in prepared["before"]
        with patch(
            "urllib.request.OpenerDirector.open",
            return_value=io.BytesIO(json.dumps(snapshot).encode()),
        ) as send:
            with pytest.raises(PlayError, match="outcome unknown"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
        send.assert_called_once()
