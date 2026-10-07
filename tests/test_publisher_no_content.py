import asyncio
import io
from unittest.mock import Mock, patch

import pytest
from mcp import Client

from pubship import http
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.publisher_reads import PublisherReads
from pubship.server import create_server

PACKAGE = "com.example.app"
METHOD = "androidpublisher.monetization.subscriptions.list"
PARAMETERS = {"packageName": PACKAGE, "pageSize": 1}
URL = "https://androidpublisher.googleapis.com/test"


def response(status, body):
    value = io.BytesIO(body)
    value.status = status
    return value


def test_no_content_requires_explicit_opt_in_and_exact_status():
    with patch("urllib.request.OpenerDirector.open", return_value=response(204, b"")) as send:
        assert http.get(URL, "synthetic", allow_no_content=True) is http.NO_CONTENT
    send.assert_called_once()
    for status, body in [(204, b""), (200, b"")]:
        with patch("urllib.request.OpenerDirector.open", return_value=response(status, body)):
            with pytest.raises(PlayError):
                http.get(URL, "synthetic")


@pytest.mark.parametrize("status,body", [(200, b""), (204, b"{}"), (204, b"PRIVATE")])
def test_empty_or_malformed_response_is_not_converted_to_no_content(status, body):
    with patch("urllib.request.OpenerDirector.open", return_value=response(status, body)) as send:
        with pytest.raises(PlayError) as caught:
            http.get(URL, "synthetic", allow_no_content=True)
    assert "PRIVATE" not in str(caught.value)
    send.assert_called_once()


@pytest.mark.parametrize("body,present", [(b"{}", True), (b"", False)])
def test_provider_representation_presence_is_explicit(body, present):
    reader = PublisherReads(
        Settings(packages=frozenset({PACKAGE})),
        Mock(token=Mock(return_value="synthetic")),
        local=True,
    )
    with patch(
        "urllib.request.OpenerDirector.open", return_value=response(200 if present else 204, body)
    ):
        result = reader.read(METHOD, PARAMETERS)
    assert result["content_present"] is present
    assert result["data"] == ({} if present else None)
    assert "total" not in result
    if not present:
        assert result["http_status"] == 204
        assert "No record count" in result["note"]
    else:
        assert "http_status" not in result


def test_json_null_is_not_http_no_content():
    reader = PublisherReads(
        Settings(packages=frozenset({PACKAGE})),
        Mock(token=Mock(return_value="synthetic")),
        local=True,
    )
    with patch("urllib.request.OpenerDirector.open", return_value=response(200, b"null")):
        with pytest.raises(PlayError, match="unexpected response"):
            reader.read(METHOD, PARAMETERS)


def test_mcp_returns_no_content_without_leaking_internal_sentinel():
    async def run():
        with patch("pubship.auth.Auth.token", return_value="synthetic"):
            async with Client(create_server(Settings(packages=frozenset({PACKAGE})))) as client:
                with patch("urllib.request.OpenerDirector.open", return_value=response(204, b"")):
                    result = await client.call_tool(
                        "read_publisher", {"method": METHOD, "parameters": PARAMETERS}
                    )
        assert not result.is_error
        assert result.structured_content["data"] is None
        assert result.structured_content["content_present"] is False
        assert result.structured_content["http_status"] == 204

    asyncio.run(run())
