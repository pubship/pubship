"""Hosted token refresh uses bounded, proxy-free, single-attempt synthetic I/O."""

import json
import ssl
import time
import urllib.request
from unittest.mock import Mock

import pytest
from google.auth.exceptions import TransportError
from google.oauth2.credentials import Credentials
from test_hosted_020_loopback import certificate
from test_provider_transport_deadlines import upstream

from pubship import hosted_token_transport as transport


def wire(data, status="200 OK", extra=b""):
    return (
        f"HTTP/1.1 {status}\r\nContent-Length: {len(data)}\r\n".encode()
        + extra
        + b"Content-Type: application/json\r\n\r\n"
        + data
    )


def call(url):
    return transport.HostedTokenRequest()(
        url,
        method="POST",
        body=b"refresh_token=synthetic",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.mark.parametrize(
    "url,method,options",
    [
        ("http://oauth2.googleapis.com/token", "POST", {}),
        ("https://oauth2.googleapis.com/token?alternate=1", "POST", {}),
        ("https://example.test/token", "POST", {}),
        (transport.TOKEN_ENDPOINT, "GET", {}),
        (transport.TOKEN_ENDPOINT, "POST", {"verify": False}),
        (transport.TOKEN_ENDPOINT, "POST", {"proxies": {}}),
    ],
)
def test_reject_other_routes_methods_or_network_overrides(monkeypatch, url, method, options):
    opener = Mock()
    monkeypatch.setattr(transport, "bounded_opener", opener)
    with pytest.raises(TransportError, match=transport.ERROR):
        transport.HostedTokenRequest()(url, method=method, body=b"synthetic", **options)
    opener.assert_not_called()


def test_google_auth_refresh_interface_and_environment_proxies_disabled(monkeypatch):
    data = json.dumps(
        {"access_token": "synthetic-access", "expires_in": 3600, "token_type": "Bearer"}
    ).encode()
    # Explicit empty ProxyHandler must suppress even environment discovery.
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        Mock(side_effect=AssertionError("environment proxies consulted")),
    )
    with upstream(wire(data)) as (url, requests):
        monkeypatch.setattr(transport, "TOKEN_ENDPOINT", url)
        credentials = Credentials(
            token=None,
            refresh_token="synthetic-refresh",
            token_uri=url,
            client_id="synthetic-client",
            client_secret="synthetic-secret",
        )
        credentials.refresh(transport.HostedTokenRequest())
        assert credentials.token == "synthetic-access"
        assert credentials.expiry is not None
        assert len(requests) == 1


@pytest.mark.parametrize(
    "status",
    [
        "400 Bad Request",
        "429 Too Many Requests",
        "500 Internal Server Error",
        "503 Service Unavailable",
    ],
)
def test_google_auth_http_failures_are_redacted_and_never_retried(monkeypatch, status):
    with upstream(
        wire(
            b'{"error":"temporarily_unavailable","error_description":"PRIVATE-PROVIDER-TEXT"}',
            status,
        )
    ) as (url, requests):
        monkeypatch.setattr(transport, "TOKEN_ENDPOINT", url)
        credentials = Credentials(
            token=None,
            refresh_token="PRIVATE-REFRESH",
            token_uri=url,
            client_id="synthetic-client",
            client_secret="PRIVATE-SECRET",
        )
        with pytest.raises(TransportError) as failure:
            credentials.refresh(transport.HostedTokenRequest())
        assert str(failure.value) == transport.ERROR
        assert len(requests) == 1


@pytest.mark.parametrize(
    "status",
    [
        "301 Moved Permanently",
        "302 Found",
        "303 See Other",
        "307 Temporary Redirect",
        "308 Permanent Redirect",
    ],
)
def test_redirects_are_blocked_without_forwarding_credentials(monkeypatch, status):
    with upstream(wire(b"", status, b"Location: http://127.0.0.1:1/PRIVATE\r\n")) as (
        url,
        requests,
    ):
        monkeypatch.setattr(transport, "TOKEN_ENDPOINT", url)
        with pytest.raises(TransportError) as failure:
            call(url)
        assert str(failure.value) == transport.ERROR
        assert len(requests) == 1


@pytest.mark.parametrize(
    "prefix,trickle",
    [
        (b"HTTP/1.1 200 ", b"x" * 100),
        (b"HTTP/1.1 200 OK\r\nX-Slow: ", b"x" * 100),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n", b"x" * 100),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;", b"x" * 100),
    ],
    ids=["status", "header", "body", "chunk-framing"],
)
def test_trickling_refresh_cannot_extend_elapsed_deadline(monkeypatch, prefix, trickle):
    monkeypatch.setattr(transport, "TIMEOUT_SECONDS", 0.15)
    with upstream(prefix, trickle) as (url, requests):
        monkeypatch.setattr(transport, "TOKEN_ENDPOINT", url)
        started = time.monotonic()
        with pytest.raises(TransportError) as failure:
            transport.HostedTokenRequest()(url, method="POST", body=b"synthetic", timeout=9999)
        assert str(failure.value) == transport.ERROR
        assert time.monotonic() - started < 0.8
        assert len(requests) == 1


@pytest.mark.parametrize("excess", [0, 1])
def test_response_body_byte_bound_and_response_interface(monkeypatch, excess):
    data = b'{"padding":"' + b"x" * (transport.MAX_RESPONSE_BYTES - 14 + excess) + b'"}'
    assert len(data) == transport.MAX_RESPONSE_BYTES + excess
    with upstream(wire(data)) as (url, requests):
        monkeypatch.setattr(transport, "TOKEN_ENDPOINT", url)
        if excess:
            with pytest.raises(TransportError) as failure:
                call(url)
            assert str(failure.value) == transport.ERROR
        else:
            response = call(url)
            assert response.status == 200
            assert response.data == data
            assert response.headers["Content-Type"] == "application/json"
            assert "padding" not in repr(response)
        assert len(requests) == 1


@pytest.mark.parametrize(
    "data", [b"PRIVATE-PROVIDER-TEXT", b"[]", b"null", b'{"value":NaN}', b"\xff"]
)
def test_invalid_success_body_is_redacted(monkeypatch, data):
    with upstream(wire(data)) as (url, requests):
        monkeypatch.setattr(transport, "TOKEN_ENDPOINT", url)
        with pytest.raises(TransportError) as failure:
            call(url)
        assert str(failure.value) == transport.ERROR
        assert len(requests) == 1


@pytest.mark.parametrize("trusted", [True, False])
def test_https_certificate_verification_is_preserved(tmp_path, monkeypatch, trusted):
    key, cert, context = certificate(tmp_path)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    original = transport.bounded_opener

    def opener(*args, **kwargs):
        result = original(*args, **kwargs)
        if trusted:
            for handler in result.handlers:
                if isinstance(handler, urllib.request.HTTPSHandler):
                    handler._context = context
        return result

    monkeypatch.setattr(transport, "bounded_opener", opener)
    with upstream(wire(b"{}"), tls_context=server, consume_body=True) as (url, requests):
        monkeypatch.setattr(transport, "TOKEN_ENDPOINT", url)
        if trusted:
            assert call(url).data == b"{}"
            assert len(requests) == 1
            assert requests[0].partition(b"\r\n\r\n")[2] == b"refresh_token=synthetic"
        else:
            with pytest.raises(TransportError) as failure:
                call(url)
            assert str(failure.value) == transport.ERROR
            assert not requests
