"""Actual urllib request bodies and errors with only synthetic network responses."""

import io
import json
import urllib.error
from email.parser import BytesParser
from email.policy import default
from unittest.mock import Mock

import pytest
from test_appstore_contracts import case

from pubship import appstore_http
from pubship.appstore_contracts import (
    APK,
    APPSTORE_METHODS,
    CREATE,
    IMAGE,
    POLICY,
    UPLOAD_METHODS,
    request_spec,
)
from pubship.artifact_files import FileRoots
from pubship.errors import PlayError
from pubship.http import NoRedirect


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.status = status


@pytest.mark.parametrize("method", sorted(APPSTORE_METHODS))
def test_single_fixed_transport_request_and_media_metadata(method, tmp_path, monkeypatch):
    p, body, mime, response = case(method)
    spec = request_spec(method, p, body, mime)
    roots, snapshot = FileRoots(str(tmp_path)), None
    if method in UPLOAD_METHODS:
        (tmp_path / "media.bin").write_bytes(b"synthetic\x00media\xff")
        snapshot = roots.snapshot("media.bin", 100)
    captured = []

    def send(req, timeout):
        captured.append(req)
        assert req.full_url == spec["url"]
        assert req.get_header("Authorization") == "Bearer SYNTHETIC_CREDENTIAL"
        assert timeout == (120 if mime else 15)
        if mime:
            chunks = []
            while chunk := req.data.read(7):
                chunks.append(chunk)
            raw = b"".join(chunks)
            assert len(raw) == int(req.get_header("Content-length"))
            if method == POLICY:
                message = BytesParser(policy=default).parsebytes(
                    (
                        "Content-Type: "
                        + req.get_header("Content-type")
                        + "\r\nMIME-Version: 1.0\r\n\r\n"
                    ).encode()
                    + raw
                )
                parts = list(message.iter_parts())
                assert len(parts) == 2
                assert json.loads(parts[0].get_payload(decode=True)) == body
                assert parts[1].get_content_type() == mime
                assert parts[1].get_payload(decode=True) == b"synthetic\x00media\xff"
            else:
                assert raw == b"synthetic\x00media\xff"
        return Response(response)

    def opener(*handlers):
        assert len(handlers) == 3 and isinstance(handlers[0], NoRedirect)
        return Mock(handlers=[], open=send)

    monkeypatch.setattr("urllib.request.build_opener", opener)
    try:
        assert appstore_http.execute(spec, "SYNTHETIC_CREDENTIAL", snapshot) == response
        assert len(captured) == 1
    finally:
        if snapshot:
            snapshot.close()
        roots.close()


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("PRIVATE"),
        urllib.error.URLError("PRIVATE"),
        urllib.error.HTTPError("https://google.test", 307, "PRIVATE", {}, io.BytesIO(b"PRIVATE")),
        Response(b"PRIVATE-not-json"),
        Response(b"{}", status=500),
        Response(b"x" * (2 * 1024 * 1024 + 1)),
        Response({"unexpected": "PRIVATE"}),
    ],
)
def test_write_errors_are_unknown_consumed_no_retry_and_redacted(failure, monkeypatch):
    p, body, mime, _ = case(CREATE)
    calls = []

    def send(*args, **kwargs):
        calls.append(args)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Mock(handlers=[], open=send))
    with pytest.raises(PlayError, match="outcome unknown") as error:
        appstore_http.execute(request_spec(CREATE, p, body, mime), "SYNTHETIC")
    assert "PRIVATE" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("method", [APK, IMAGE, POLICY])
def test_upload_does_not_accept_missing_id_or_empty_204(method, tmp_path, monkeypatch):
    p, body, mime, _ = case(method)
    roots = FileRoots(str(tmp_path))
    (tmp_path / "media.bin").write_bytes(b"media")
    snapshot = roots.snapshot("media.bin", 100)
    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *args: Mock(handlers=[], open=lambda *a, **kw: Response(b"", 204)),
    )
    with pytest.raises(PlayError, match="outcome unknown"):
        appstore_http.execute(request_spec(method, p, body, mime), "SYNTHETIC", snapshot)
    snapshot.close()
    roots.close()


def test_caller_url_cannot_redirect_transport(monkeypatch):
    p, body, mime, _ = case(CREATE)
    spec = request_spec(CREATE, p, body, mime)
    spec["url"] = "http://evil.test/steal"

    def send(req, **kwargs):
        assert req.full_url.startswith("https://androidpublisher.googleapis.com/")
        return Response(b"", 204)

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Mock(handlers=[], open=send))
    assert appstore_http.execute(spec, "SYNTHETIC") == {}
