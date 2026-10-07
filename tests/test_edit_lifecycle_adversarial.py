"""Independent lifecycle acceptance with synthetic Google and real stdio clients."""

import asyncio
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from pubship.config import Settings
from pubship.edit_lifecycle import EditLifecycle
from pubship.edit_lifecycle_http import execute
from pubship.errors import PlayError
from pubship.publishing import Publishing
from pubship.server import create_server

APP = "com.example.app"
PARAMS = {"packageName": APP, "editId": "edit-1"}


def configured():
    return Settings(
        packages=frozenset({APP}), write_packages=frozenset({APP}), edit_packages=frozenset({APP})
    )


def fixture_service():
    auth = Mock()
    auth.token.return_value = "synthetic-original-private"
    service = EditLifecycle(configured(), auth, local=True)
    edit = {"id": "edit-1", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    return service, auth, edit


@pytest.mark.parametrize(
    "action,parameters,ack",
    [
        ("delete", PARAMS, True),
        ("commit", {**PARAMS, "changesInReviewBehavior": "CANCEL_IN_REVIEW_AND_SUBMIT"}, True),
        ("commit", {**PARAMS, "changesInReviewBehavior": None}, True),
        ("commit", {**PARAMS, "changesNotSentForReview": "false"}, True),
        ("commit", {**PARAMS, "changesNotSentForReview": 1}, True),
        ("commit", {**PARAMS, "changesNotSentForReview": None}, True),
        ("validate", {**PARAMS, "changesNotSentForReview": False}, True),
        ("discard", {**PARAMS, "body": {}}, True),
        ("create", PARAMS, True),
        ("create", {"packageName": "com.other.app"}, True),
        ("commit", {**PARAMS, "editId": "x:commit?injected=yes"}, True),
        ("commit", PARAMS, "true"),
        ("commit", PARAMS, 1),
        ("commit", PARAMS, False),
        ("create", {"packageName": APP}, False),
    ],
)
def test_invalid_lifecycle_requests_fail_before_credentials_or_provider(action, parameters, ack):
    service, auth, _ = fixture_service()
    with (
        patch("pubship.http.get") as read,
        patch("pubship.edit_lifecycle_http.execute") as write,
    ):
        with pytest.raises(PlayError):
            service.prepare(action, parameters, ack)
    auth.token.assert_not_called()
    read.assert_not_called()
    write.assert_not_called()


def test_preparation_binds_flags_target_original_token_and_whole_edit_effects():
    service, auth, edit = fixture_service()
    parameters = {**PARAMS, "changesNotSentForReview": True}
    with patch("pubship.http.get", return_value=edit) as read:
        prepared = service.prepare("commit", parameters, True)
        assert prepared["scope"] == "entire_edit"
        assert "Play Console" in prepared["effects"]
        assert prepared["full_edit_contents_verified"] is False
        parameters["changesNotSentForReview"] = False
        prepared["proposed_request"]["parameters"]["editId"] = "unreviewed"
        auth.token.return_value = "different-account"
        with patch("pubship.edit_lifecycle_http.execute", return_value=edit) as write:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_called_once_with(
            "commit",
            {
                **PARAMS,
                "changesNotSentForReview": True,
                "changesInReviewBehavior": "ERROR_IF_IN_REVIEW",
            },
            "synthetic-original-private",
        )
        assert all(call.args[1] == "synthetic-original-private" for call in read.call_args_list)
    auth.token.assert_called_once_with("publisher")
    assert result["committed"] is True
    assert result["publication_verified"] is False
    assert result["content_drift_checked"] is False
    assert "synthetic-original-private" not in json.dumps(result) + json.dumps(prepared)


@pytest.mark.parametrize("action", ["validate", "discard", "commit"])
def test_changed_metadata_consumes_preparation_without_dispatch(action):
    service, _, edit = fixture_service()
    with patch(
        "pubship.http.get",
        side_effect=[edit, {**edit, "expiryTimeSeconds": str(int(edit["expiryTimeSeconds"]) + 1)}],
    ) as read:
        prepared = service.prepare(action, PARAMS, True)
        with patch("pubship.edit_lifecycle_http.execute") as write:
            with pytest.raises(PlayError, match="metadata changed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_not_called()
    assert read.call_count == 2


def test_listing_and_lifecycle_preparations_do_not_cross_authorize():
    lifecycle, auth, edit = fixture_service()
    staging = Publishing(configured(), auth, local=True)
    listing = {"language": "en-US", "title": "old"}
    with patch("pubship.http.get", side_effect=[edit, listing]):
        stage = staging.prepare({**PARAMS, "language": "en-US"}, {"title": "new"})
    life = lifecycle.prepare("create", {"packageName": APP}, True)
    with patch("pubship.edit_lifecycle_http.execute") as mutation:
        with pytest.raises(PlayError):
            lifecycle.apply(stage["operation_id"], stage["operation_id"])
        with pytest.raises(PlayError):
            staging.apply(life["operation_id"], life["operation_id"])
    mutation.assert_not_called()


def test_lifecycle_cannot_dispatch_while_listing_patch_is_in_flight():
    lifecycle, auth, edit = fixture_service()
    staging = Publishing(configured(), auth, local=True)
    listing = {"language": "en-US", "title": "old"}
    with patch("pubship.http.get", side_effect=[edit, listing, edit]):
        stage = staging.prepare({**PARAMS, "language": "en-US"}, {"title": "new"})
        life = lifecycle.prepare("commit", PARAMS, True)
    entered, release = threading.Event(), threading.Event()

    def slow_patch(*args):
        entered.set()
        assert release.wait(5)
        return {**listing, "title": "new"}

    with (
        patch("pubship.http.get", side_effect=[edit, listing]) as read,
        patch("pubship.publishing_http.listing_patch", side_effect=slow_patch),
        patch("pubship.edit_lifecycle_http.execute") as commit,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(staging.apply, stage["operation_id"], stage["operation_id"])
        try:
            assert entered.wait(2)
            with pytest.raises(PlayError, match="in progress"):
                lifecycle.apply(life["operation_id"], life["operation_id"])
            commit.assert_not_called()
            assert read.call_count == 2
        finally:
            release.set()
        assert pending.result(timeout=2)["executed"]
    with pytest.raises(PlayError, match="consumed"):
        lifecycle.apply(life["operation_id"], life["operation_id"])


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 410, 429, 500, 503])
def test_commit_error_never_cancels_review_or_retries(code):
    error = HTTPError(
        "https://androidpublisher.googleapis.com/",
        code,
        "PRIVATE-DETAIL",
        {},
        io.BytesIO(b"PRIVATE-TOKEN"),
    )
    with patch("urllib.request.OpenerDirector.open", side_effect=error) as send:
        with pytest.raises(PlayError, match="outcome unknown") as caught:
            execute("commit", PARAMS, "synthetic-token")
    send.assert_called_once()
    request = send.call_args.args[0]
    assert "changesInReviewBehavior=ERROR_IF_IN_REVIEW" in request.full_url
    assert "changesNotSentForReview=false" in request.full_url
    assert request.data is None
    assert "PRIVATE" not in str(caught.value)
    assert "No writes were attempted" not in str(caught.value)


@pytest.mark.parametrize("enabled", [False, True])
def test_old_staging_opt_in_and_hosted_never_gain_lifecycle_tools(enabled, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_WRITE_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_EDIT_PACKAGES", APP)
    factory = Mock(side_effect=AssertionError("No credentials needed for tool discovery"))

    async def exercise():
        server = (
            create_server(service_factory=factory)
            if enabled
            else create_server(Settings(packages=frozenset({APP}), write_packages=frozenset({APP})))
        )
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "prepare_publisher_edit_operation" not in names
            assert "apply_publisher_edit_operation" not in names

    asyncio.run(exercise())
    factory.assert_not_called()


def test_actual_stdio_create_stage_validate_commit_and_discard(tmp_path):
    launcher = tmp_path / "lifecycle_stdio.py"
    launcher.write_text("""
import io, json, time
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch
from pubship.auth import Auth
from pubship.server import main
base = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.example.app/edits"
state = {"counter":0, "edit":None, "listing":{"language":"en-US", "title":"old"}}
def send(self, request, timeout):
    assert request.get_header("Authorization") == "Bearer synthetic-stdio"
    assert timeout == 15
    method = request.get_method()
    url = request.full_url
    if url == base and method == "POST":
        assert request.data == b"{}"
        state["counter"]+=1
        state["edit"]={"id":"edit-"+str(state["counter"]),"expiryTimeSeconds":str(int(time.time())+3600)}
        data=state["edit"]
    else:
        assert state["edit"] is not None
        editurl=base+"/"+state["edit"]["id"]
        if url == editurl and method=="GET":
            assert request.data is None
            data=state["edit"]
        elif url == editurl+"/listings/en-US":
            if method=="PATCH":
                assert json.loads(request.data)=={"title":"new"}
                state["listing"].update(json.loads(request.data))
            else:
                assert method=="GET" and request.data is None
            data=state["listing"]
        elif url == editurl+":validate":
            assert method=="POST" and request.data is None
            data=state["edit"]
        elif url.startswith(editurl+":commit?"):
            assert method=="POST" and request.data is None
            assert parse_qs(urlsplit(url).query)=={"changesNotSentForReview":["false"],"changesInReviewBehavior":["ERROR_IF_IN_REVIEW"]}
            data=state["edit"]
            state["edit"]=None
        elif url==editurl and method=="DELETE":
            assert request.data is None
            state["edit"]=None
            data={}
        else:
            raise AssertionError("Unexpected provider request")
    return io.BytesIO(json.dumps(data).encode())
with patch.object(Auth,"token",return_value="synthetic-stdio"), patch("urllib.request.OpenerDirector.open",send):
    main()
""")

    async def exercise():
        transport = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher)],
            env={
                "GOOGLE_PLAY_PACKAGES": APP,
                "GOOGLE_PLAY_WRITE_PACKAGES": APP,
                "GOOGLE_PLAY_EDIT_PACKAGES": APP,
                "GOOGLE_PLAY_AUTH_MODE": "adc",
            },
        )
        async with Client(transport) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert len(tools) == 17
            apply_tool = tools["apply_publisher_edit_operation"]
            assert apply_tool.annotations.destructive_hint
            assert not apply_tool.annotations.read_only_hint

            async def lifecycle(action, params):
                prepared = await client.call_tool(
                    "prepare_publisher_edit_operation",
                    {"action": action, "parameters": params, "acknowledge_effects": True},
                )
                assert not prepared.is_error, prepared.content
                identifier = prepared.structured_content["operation_id"]
                result = await client.call_tool(
                    apply_tool.name, {"operation_id": identifier, "confirmation": identifier}
                )
                assert not result.is_error, result.content
                replay = await client.call_tool(
                    apply_tool.name, {"operation_id": identifier, "confirmation": identifier}
                )
                assert replay.is_error
                assert result.structured_content["publication_verified"] is False
                return result.structured_content

            created = await lifecycle("create", {"packageName": APP})
            params = {"packageName": APP, "editId": created["edit_id"]}
            prepared = await client.call_tool(
                "prepare_store_listing_update",
                {"parameters": {**params, "language": "en-US"}, "changes": {"title": "new"}},
            )
            assert not prepared.is_error, prepared.content
            identifier = prepared.structured_content["operation_id"]
            staged = await client.call_tool(
                "apply_store_listing_update",
                {"operation_id": identifier, "confirmation": identifier},
            )
            assert not staged.is_error and staged.structured_content["data"]["title"] == "new"
            validated = await lifecycle("validate", params)
            assert validated["provider_validation_performed"] and not validated["committed"]
            committed = await lifecycle("commit", params)
            assert committed["committed"] and not committed["full_edit_contents_verified"]
            created = await lifecycle("create", {"packageName": APP})
            discarded = await lifecycle(
                "discard", {"packageName": APP, "editId": created["edit_id"]}
            )
            assert discarded["discarded"]

    asyncio.run(exercise())


def test_mcp_lifecycle_schema_rejects_coercion_and_extra_inputs_before_auth():
    async def exercise():
        async with Client(create_server(configured())) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            prepare = tools["prepare_publisher_edit_operation"]
            apply = tools["apply_publisher_edit_operation"]
            assert prepare.input_schema["additionalProperties"] is False
            assert apply.input_schema["additionalProperties"] is False
            with patch(
                "pubship.auth.Auth.token",
                side_effect=AssertionError("Must reject before auth"),
            ) as auth:
                for invalid in (
                    {
                        "action": "create",
                        "parameters": {"packageName": APP},
                        "acknowledge_effects": "true",
                    },
                    {
                        "action": "create",
                        "parameters": {"packageName": APP},
                        "acknowledge_effects": 1,
                    },
                    {
                        "action": "create",
                        "parameters": {"packageName": APP},
                        "acknowledge_effects": True,
                        "body": "PRIVATE",
                    },
                    {"action": "create", "parameters": {"packageName": APP}},
                ):
                    result = await client.call_tool(prepare.name, invalid)
                    assert result.is_error
                    assert "PRIVATE" not in str(result.content)
                result = await client.call_tool(
                    apply.name,
                    {"operation_id": "invalid", "confirmation": "invalid", "action": "PRIVATE"},
                )
                assert result.is_error and "PRIVATE" not in str(result.content)
                auth.assert_not_called()
            with (
                patch("pubship.auth.Auth.token", return_value="synthetic"),
                patch(
                    "pubship.edit_lifecycle_http.execute",
                    return_value={
                        "id": "new-edit",
                        "expiryTimeSeconds": str(int(time.time()) + 3600),
                    },
                ),
            ):
                prepared = await client.call_tool(
                    prepare.name,
                    {
                        "action": "create",
                        "parameters": {"packageName": APP},
                        "acknowledge_effects": True,
                    },
                )
                assert not prepared.is_error
                identifier = prepared.structured_content["operation_id"]
                result = await client.call_tool(
                    apply.name, {"operation_id": identifier, "confirmation": identifier}
                )
                assert not result.is_error and result.structured_content["created"]

    asyncio.run(exercise())
