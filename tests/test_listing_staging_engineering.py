import asyncio
import json
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
from mcp import Client

from pubship import publishing_http
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.publishing import Publishing
from pubship.server import create_server

PARAMETERS = {"packageName": "com.example.app", "editId": "edit-1", "language": "en-US"}
URL = (
    "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
    "com.example.app/edits/edit-1/listings/en-US"
)
SETTINGS = Settings(
    packages=frozenset({"com.example.app"}), write_packages=frozenset({"com.example.app"})
)


@pytest.fixture
def service():
    return Publishing(
        SETTINGS, Mock(token=Mock(return_value="synthetic-private-token")), local=True
    )


@pytest.fixture
def edit():
    return {"id": "edit-1", "expiryTimeSeconds": str(int(time.time()) + 3600)}


def test_retains_identity_and_exact_body_and_actual_provider_result(service, edit):
    current = {"language": "en-US", "title": "old", "video": "unchanged", "extra": [1]}
    body = {"title": "  新 🐈\r\n"}
    with (
        patch("pubship.publishing.http.get", side_effect=[edit, current, edit, current]) as get,
        patch(
            "pubship.publishing_http.listing_patch",
            return_value={"language": "en-US", "title": "provider value"},
        ) as write,
    ):
        prepared = service.prepare(PARAMETERS, body)
        body["title"] = "mutated by caller"
        prepared["proposed_request"]["body"]["title"] = "mutated public result"
        service.auth.token.return_value = "changed-principal"
        result = service.apply(prepared["operation_id"], prepared["operation_id"])
    service.auth.token.assert_called_once_with("publisher")
    assert all(call.args[1] == "synthetic-private-token" for call in get.call_args_list)
    write.assert_called_once_with(URL, "synthetic-private-token", {"title": "  新 🐈\r\n"})
    assert result["data"]["title"] == "provider value"
    assert result["executed"] and result["patch_attempted"]
    assert not result["committed"] and not result["publication_verified"]
    assert "synthetic-private-token" not in json.dumps(prepared)
    with pytest.raises(PlayError, match="consumed"):
        service.apply(prepared["operation_id"], prepared["operation_id"])


@pytest.mark.parametrize(
    "current",
    [
        {"language": "en-US", "title": "old", "video": "changed"},
        {"language": "en-US", "title": "old", "extra": True},
        {"language": "en-US", "title": "old", "extra": 1.0},
    ],
)
def test_full_listing_baseline_including_types_is_checked(service, edit, current):
    before = {"language": "en-US", "title": "old", "extra": 1}
    with (
        patch("pubship.publishing.http.get", side_effect=[edit, before, edit, current]),
        patch("pubship.publishing_http.listing_patch") as write,
    ):
        operation = service.prepare(PARAMETERS, {"title": "new"})["operation_id"]
        with pytest.raises(PlayError, match="baseline changed"):
            service.apply(operation, operation)
        write.assert_not_called()


def test_missing_and_empty_are_distinct_and_noop_skips_patch(service, edit):
    baseline = {"language": "en-US", "title": ""}
    with (
        patch("pubship.publishing.http.get", side_effect=[edit, baseline, edit, baseline]),
        patch("pubship.publishing_http.listing_patch") as write,
    ):
        operation = service.prepare(PARAMETERS, {"title": ""})["operation_id"]
        result = service.apply(operation, operation)
    assert result["no_op"] and not result["executed"] and not result["patch_attempted"]
    write.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"id": "wrong", "expiryTimeSeconds": "9999999999"},
        {"id": "edit-1", "expiryTimeSeconds": "0"},
        {"id": "edit-1", "expiryTimeSeconds": "nan"},
        {"id": "edit-1", "expiryTimeSeconds": 9999999999},
    ],
)
def test_invalid_edit_metadata_never_reads_listing(service, metadata):
    with patch("pubship.publishing.http.get", return_value=metadata) as get:
        with pytest.raises(PlayError):
            service.prepare(PARAMETERS, {"title": "new"})
        assert get.call_count == 1


def test_expiry_and_capacity_are_bounded_before_provider_io(service, edit):
    with (
        patch("pubship.publishing.MAX_PREPARATIONS", 1),
        patch("pubship.publishing.http.get", side_effect=[edit, {"language": "en-US"}]) as get,
    ):
        operation = service.prepare(PARAMETERS, {"title": "new"})["operation_id"]
        with pytest.raises(PlayError, match="capacity"):
            service.prepare(PARAMETERS, {"title": "new"})
        service._preparations[operation].deadline = time.monotonic() - 1
        with pytest.raises(PlayError, match="expired"):
            service.apply(operation, operation)
        assert get.call_count == 2


def test_simultaneous_replay_dispatches_only_once(service, edit):
    listing = {"language": "en-US"}
    with (
        patch("pubship.publishing.http.get", side_effect=[edit, listing, edit, listing]),
        patch(
            "pubship.publishing_http.listing_patch",
            return_value={**listing, "title": "new"},
        ) as write,
    ):
        operation = service.prepare(PARAMETERS, {"title": "new"})["operation_id"]

        def apply():
            try:
                return service.apply(operation, operation)
            except PlayError:
                return None

        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda _: apply(), range(2)))
        assert sum(result is not None for result in results) == 1
        write.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [None, [], {}, {"language": "de-DE"}, {"language": "en-US"}, {"language": "en-US", "title": 4}],
)
def test_malformed_success_is_unknown_and_consumed(service, edit, response):
    with (
        patch("pubship.publishing.http.get", side_effect=[edit, {"language": "en-US"}] * 2),
        patch("pubship.publishing_http.listing_patch", return_value=response),
    ):
        operation = service.prepare(PARAMETERS, {"title": "new"})["operation_id"]
        with pytest.raises(PlayError, match="outcome unknown"):
            service.apply(operation, operation)
        with pytest.raises(PlayError, match="consumed"):
            service.apply(operation, operation)


def test_disabled_service_and_write_scope_reject_before_auth():
    auth = Mock()
    for service in [Publishing(SETTINGS, auth), Publishing(Settings(), auth, local=True)]:
        with pytest.raises(PlayError, match="explicitly enabled"):
            service.prepare(PARAMETERS, {"title": "new"})
    auth.token.assert_not_called()
    with pytest.raises(PlayError, match="WRITE_PACKAGES"):
        Settings(write_packages=frozenset({"com.example.app"}))


def test_tools_only_local_optin_and_apply_schema_is_strict():
    async def exercise():
        for server, enabled in [
            (create_server(Settings()), False),
            (create_server(service_factory=Mock()), False),
            (create_server(SETTINGS), True),
        ]:
            async with Client(server) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert ("apply_store_listing_update" in tools) is enabled
                if enabled:
                    assert not tools["apply_store_listing_update"].annotations.read_only_hint
                    assert (
                        tools["apply_store_listing_update"].input_schema["additionalProperties"]
                        is False
                    )
                    for confirmation in [True, 123, None, "yes"]:
                        result = await client.call_tool(
                            "apply_store_listing_update",
                            {"operation_id": "x" * 43, "confirmation": confirmation},
                        )
                        assert result.is_error

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "url",
    [
        URL + "?x=1",
        URL + "#frag",
        URL + "?",
        URL.replace("en-US", ".."),
        URL.replace("googleapis.com", "googleapis.com.evil"),
        URL.replace("edit-1", "%2fescape"),
        "\n" + URL,
    ],
)
def test_transport_rejects_scope_escape_before_dispatch(url):
    with patch("urllib.request.OpenerDirector.open") as request:
        with pytest.raises(PlayError):
            publishing_http.listing_patch(url, "synthetic", {"title": "new"})
        request.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("PRIVATE"),
        urllib.error.URLError("PRIVATE"),
        PlayError("Unexpected redirect PRIVATE"),
        urllib.error.HTTPError(URL, 503, "PRIVATE", {}, None),
    ],
)
def test_transport_errors_are_redacted_uncertain_and_single_attempt(failure):
    with patch("urllib.request.OpenerDirector.open", side_effect=failure) as request:
        with pytest.raises(PlayError, match="outcome unknown") as error:
            publishing_http.listing_patch(URL, "synthetic", {"title": "new"})
    assert "PRIVATE" not in str(error.value)
    assert "No writes" not in str(error.value)
    request.assert_called_once()


def test_wire_method_token_body_and_finite_response():
    response = Mock()
    response.read.return_value = b'{"language":"en-US","title":"new"}'
    context = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
    with patch("urllib.request.OpenerDirector.open", return_value=context) as request:
        assert publishing_http.listing_patch(URL, "synthetic", {"title": "new"})["title"] == "new"
    sent = request.call_args.args[0]
    assert sent.get_method() == "PATCH"
    assert json.loads(sent.data) == {"title": "new"}
    assert sent.headers["Authorization"] == "Bearer synthetic"
    assert request.call_args.kwargs == {"timeout": 15}
    response.read.assert_called_once_with(20 * 1024 * 1024 + 1)


@pytest.mark.parametrize("oversize_edit", [True, False])
def test_retained_provider_snapshots_are_bounded(service, edit, oversize_edit):
    listing = {"language": "en-US"}
    (edit if oversize_edit else listing)["unexpected"] = "x" * (64 * 1024)
    with patch("pubship.publishing.http.get", side_effect=[edit, listing]):
        with pytest.raises(PlayError, match="64 KiB preparation limit"):
            service.prepare(PARAMETERS, {"title": "new"})
    assert service._preparations == {}
