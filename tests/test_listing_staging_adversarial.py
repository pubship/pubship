"""Independent staging acceptance: synthetic providers, including real stdio wire I/O."""

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
from pubship.errors import PlayError
from pubship.publishing import Publishing
from pubship.publishing_http import listing_patch
from pubship.server import create_server

APP = "com.example.app"
PARAMS = {"packageName": APP, "editId": "owned-edit", "language": "en-US"}
URL = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{APP}/edits/owned-edit/listings/en-US"


def fixture_service():
    auth = Mock()
    auth.token.return_value = "private-synthetic-original-token"
    service = Publishing(
        Settings(packages=frozenset({APP}), write_packages=frozenset({APP})), auth, local=True
    )
    edit = {"id": "owned-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    listing = {"language": "en-US", "title": "old", "video": "unchanged"}
    return service, auth, edit, listing


def test_retained_original_identity_body_and_target_despite_mutated_input():
    service, auth, edit, listing = fixture_service()
    params = dict(PARAMS)
    changes = {"title": "  Café e\u0301\n🐈  "}
    with patch("pubship.http.get", side_effect=[edit, listing, edit, listing]) as read:
        prepared = service.prepare(params, changes)
        params["editId"] = "different-edit"
        changes["title"] = "NOT-REVIEWED"
        prepared["proposed_request"]["body"]["title"] = "ALSO-NOT-REVIEWED"
        auth.token.return_value = "different-account-token"
        response = {**listing, "title": "  Café e\u0301\n🐈  "}
        with patch("pubship.publishing_http.listing_patch", return_value=response) as write:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_called_once_with(
            URL, "private-synthetic-original-token", {"title": response["title"]}
        )
    auth.token.assert_called_once_with("publisher")
    assert all(call.args[1] == "private-synthetic-original-token" for call in read.call_args_list)
    assert result["data"] == response
    assert result["committed"] is False and result["publication_verified"] is False
    assert "private-synthetic" not in json.dumps(result) + json.dumps(prepared)


@pytest.mark.parametrize(
    "current",
    [
        {"language": "en-US", "title": "old", "video": "changed"},
        {"language": "en-US", "title": "old"},
        {"language": "en-US", "title": "old", "video": "unchanged", "newField": ""},
        {"language": "en-US", "title": "new", "video": "unchanged"},
    ],
)
def test_whole_snapshot_drift_consumes_id_without_mutation(current):
    service, _, edit, listing = fixture_service()
    with patch("pubship.http.get", side_effect=[edit, listing, edit, current]):
        prepared = service.prepare(PARAMS, {"title": "new"})
        with patch("pubship.publishing_http.listing_patch") as write:
            with pytest.raises(PlayError, match="baseline changed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_not_called()


def test_concurrent_same_preparation_dispatches_one_patch_only():
    service, _, edit, listing = fixture_service()
    with patch("pubship.http.get", side_effect=[edit, listing, edit, listing]):
        prepared = service.prepare(PARAMS, {"title": "new"})

        def attempt():
            try:
                return service.apply(prepared["operation_id"], prepared["operation_id"])
            except PlayError:
                return None

        with patch(
            "pubship.publishing_http.listing_patch",
            return_value={**listing, "title": "new"},
        ) as write:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: attempt(), range(2)))
        write.assert_called_once()
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize(
    "response", [None, [], {}, {"language": "fr-FR"}, {"language": "en-US", "title": None}]
)
def test_invalid_response_after_patch_is_unknown_and_cannot_replay(response):
    service, _, edit, listing = fixture_service()
    with patch("pubship.http.get", side_effect=[edit, listing, edit, listing]):
        prepared = service.prepare(PARAMS, {"title": "new"})
        with patch("pubship.publishing_http.listing_patch", return_value=response) as write:
            with pytest.raises(PlayError, match="outcome unknown") as error:
                service.apply(prepared["operation_id"], prepared["operation_id"])
            assert "No writes were attempted" not in str(error.value)
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
        write.assert_called_once()


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 410, 429, 500, 503])
def test_patch_http_errors_redact_provider_details_and_never_retry(code):
    error = HTTPError(URL, code, "PRIVATE-PROVIDER-DETAIL", {}, io.BytesIO(b"PRIVATE-TOKEN"))
    with patch("urllib.request.OpenerDirector.open", side_effect=error) as send:
        with pytest.raises(PlayError, match="outcome unknown") as result:
            listing_patch(URL, "private-synthetic-token", {"title": "new"})
    send.assert_called_once()
    assert f"HTTP {code}" in str(result.value)
    assert "PRIVATE" not in str(result.value) and "private-synthetic" not in str(result.value)
    assert "No writes were attempted" not in str(result.value)


def test_hosted_factory_never_exposes_local_write_tools_even_with_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_WRITE_PACKAGES", APP)
    factory = Mock(side_effect=AssertionError("No customer credentials for discovery"))

    async def check():
        async with Client(create_server(service_factory=factory)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "prepare_store_listing_update" not in names
            assert "apply_store_listing_update" not in names
            result = await client.call_tool(
                "apply_store_listing_update", {"operation_id": "x", "confirmation": "x"}
            )
            assert result.is_error

    asyncio.run(check())
    factory.assert_not_called()


def test_real_stdio_prepare_apply_with_synthetic_http_and_no_credentials(tmp_path):
    # The child intercepts the actual urllib boundary. Any route outside this
    # exact synthetic edit fails instead of reaching a provider.
    launcher = tmp_path / "synthetic_stdio.py"
    launcher.write_text("""
import io, json, time
from unittest.mock import patch
from pubship.auth import Auth
from pubship.server import main
listing = {"language": "en-US", "title": "old"}
edit = {"id": "owned-edit", "expiryTimeSeconds": str(int(time.time())+3600)}
base = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.example.app/edits/owned-edit"
def send(self, request, timeout):
    assert request.get_header("Authorization") == "Bearer synthetic-stdio-token"
    assert timeout == 15
    if request.full_url == base and request.get_method() == "GET":
        data = edit
    elif request.full_url == base + "/listings/en-US":
        if request.get_method() == "PATCH":
            assert json.loads(request.data) == {"title": "new"}
            listing.update(json.loads(request.data))
        else:
            assert request.get_method() == "GET" and request.data is None
        data = listing
    else:
        raise AssertionError("Unexpected provider route")
    return io.BytesIO(json.dumps(data).encode())
with patch.object(Auth, "token", return_value="synthetic-stdio-token"), patch("urllib.request.OpenerDirector.open", send):
    main()
""")

    async def check():
        connection = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher)],
            env={
                "GOOGLE_PLAY_PACKAGES": APP,
                "GOOGLE_PLAY_WRITE_PACKAGES": APP,
                "GOOGLE_PLAY_AUTH_MODE": "adc",
            },
        )
        async with Client(connection) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert len(tools) == 15
            tool = tools["apply_store_listing_update"]
            assert not tool.annotations.read_only_hint and tool.annotations.destructive_hint
            assert not tool.annotations.idempotent_hint
            prepared = await client.call_tool(
                "prepare_store_listing_update", {"parameters": PARAMS, "changes": {"title": "new"}}
            )
            assert not prepared.is_error, prepared.content
            identifier = prepared.structured_content["operation_id"]
            extra = await client.call_tool(
                tool.name,
                {"operation_id": identifier, "confirmation": identifier, "body": "PRIVATE"},
            )
            assert extra.is_error and "PRIVATE" not in str(extra.content)
            result = await client.call_tool(
                tool.name, {"operation_id": identifier, "confirmation": identifier}
            )
            assert not result.is_error, result.content
            assert result.structured_content["data"]["title"] == "new"
            assert result.structured_content["executed"] is True
            assert result.structured_content["committed"] is False
            replay = await client.call_tool(
                tool.name, {"operation_id": identifier, "confirmation": identifier}
            )
            assert replay.is_error

    asyncio.run(check())


def test_slow_prepare_does_not_block_apply_of_an_existing_preparation():
    service, _, edit, listing = fixture_service()
    with patch("pubship.http.get", side_effect=[edit, listing]):
        prepared = service.prepare(PARAMS, {"title": "new"})
    entered, release = threading.Event(), threading.Event()

    def read(url, token):
        if threading.current_thread().name.startswith("slow-prepare"):
            entered.set()
            assert release.wait(5), "Test did not release the synthetic provider"
        return edit if url.endswith("/owned-edit") else listing

    with (
        patch("pubship.http.get", side_effect=read),
        patch(
            "pubship.publishing_http.listing_patch",
            return_value={**listing, "title": "new"},
        ) as write,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow-prepare") as preparing,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="independent-apply") as applying,
    ):
        pending = preparing.submit(service.prepare, PARAMS, {"title": "another"})
        try:
            assert entered.wait(2)
            result = applying.submit(
                service.apply, prepared["operation_id"], prepared["operation_id"]
            ).result(timeout=2)
            assert result["executed"] is True
            write.assert_called_once()
        finally:
            release.set()
        assert pending.result(timeout=2)["operation_id"] != prepared["operation_id"]
