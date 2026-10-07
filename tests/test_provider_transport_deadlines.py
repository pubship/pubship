"""Synthetic loopback evidence for total read deadlines and final dispatch checks."""

import io
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_hosted_020_loopback import certificate

from pubship import (
    artifact_http,
    edit_lifecycle_http,
    edit_writes_http,
    monetization_http,
    provider_transport,
    publishing_http,
    purchase_http,
    signing,
    support_http,
    track_staging_http,
)
from pubship.errors import PlayError
from pubship.http import NoRedirect
from pubship.provider_transport import bounded_opener


@contextmanager
def upstream(prefix, trickle=b"", *, interval=0.02, tls_context=None, consume_body=False):
    """One local request, no credentials or provider network access."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    stop, requests = threading.Event(), []

    def serve():
        try:
            connection, _ = listener.accept()
            if tls_context is not None:
                connection = tls_context.wrap_socket(connection, server_side=True)
            with connection:
                connection.settimeout(2)
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                if consume_body:
                    # TLS POST headers and body can arrive in separate records.
                    # Closing with unread request data resets TCP on Linux and
                    # discards an otherwise valid synthetic response.
                    headers, _, body = request.partition(b"\r\n\r\n")
                    length = next(
                        int(line.split(b":", 1)[1].strip())
                        for line in headers.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    remaining = length - len(body)
                    while remaining > 0:
                        chunk = connection.recv(min(4096, remaining))
                        if not chunk:
                            return
                        request.extend(chunk)
                        remaining -= len(chunk)
                requests.append(bytes(request))
                connection.sendall(prefix)
                for byte in trickle:
                    if stop.wait(interval):
                        break
                    connection.sendall(bytes([byte]))
        except OSError:
            pass  # The expected deadline closes the client socket.

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        scheme = "https" if tls_context is not None else "http"
        yield f"{scheme}://127.0.0.1:{listener.getsockname()[1]}/synthetic", requests
    finally:
        stop.set()
        listener.close()
        worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.mark.parametrize(
    "prefix,trickle",
    [
        (b"HTTP/1.1 200 ", b"x" * 100),
        (b"HTTP/1.1 200 OK\r\nX-Slow: ", b"x" * 100),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n", b"x" * 100),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;", b"x" * 100),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\nX-Trailer: ",
            b"x" * 100,
        ),
    ],
    ids=["status", "header", "body", "chunk-header", "chunk-trailer"],
)
def test_trickling_cannot_extend_absolute_read_deadline(prefix, trickle):
    with upstream(prefix, trickle) as (url, requests):
        started = time.monotonic()
        opener = bounded_opener(NoRedirect(), timeout=0.2, total_timeout=0.35)
        with pytest.raises((TimeoutError, urllib.error.URLError)):
            with opener.open(url, timeout=0.2) as response:
                response.read(1024)
        assert time.monotonic() - started < 1.2
        assert len(requests) == 1


@pytest.mark.parametrize(
    "wire,expected",
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc", b"abc"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n", b"abc"),
        (b"HTTP/1.1 204 No Content\r\n\r\n", b""),
    ],
)
def test_bounded_reader_preserves_completed_http_representations(wire, expected):
    with upstream(wire) as (url, requests):
        with bounded_opener(NoRedirect(), timeout=1).open(url, timeout=1) as response:
            assert response.read(1024) == expected
        assert response.closed and len(requests) == 1


@pytest.mark.parametrize("failure", [None, "slow-headers", "untrusted-certificate"])
def test_https_preserves_certificate_checks_and_header_deadlines(tmp_path, failure):
    key, cert, trusted_context = certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    prefix, trickle = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc", b""
    if failure == "slow-headers":
        prefix, trickle = b"HTTP/1.1 200 OK\r\nX-Slow: ", b"x" * 100
    with upstream(prefix, trickle, tls_context=server_context) as (url, requests):
        opener = bounded_opener(NoRedirect(), timeout=0.2, total_timeout=0.35)
        if failure != "untrusted-certificate":
            for handler in opener.handlers:
                if isinstance(handler, urllib.request.HTTPSHandler):
                    handler._context = trusted_context
        started = time.monotonic()
        if failure:
            with pytest.raises((TimeoutError, urllib.error.URLError)):
                opener.open(url, timeout=0.2)
            assert time.monotonic() - started < 1.2
            assert len(requests) == (0 if failure == "untrusted-certificate" else 1)
        else:
            with opener.open(url, timeout=0.2) as response:
                assert response.read() == b"abc"


def test_delayed_dns_cannot_send_request_after_returning_past_deadline(monkeypatch):
    original = socket.getaddrinfo

    def delayed(*args, **kwargs):
        time.sleep(0.2)
        return original(*args, **kwargs)

    with upstream(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") as (url, requests):
        monkeypatch.setattr(socket, "getaddrinfo", delayed)
        opener = bounded_opener(NoRedirect(), timeout=1, total_timeout=0.1)
        with pytest.raises(urllib.error.URLError):
            opener.open(url, timeout=1)
    assert not requests


@pytest.mark.parametrize("late_delay", [6.0, 9.0], ids=["remaining-budget", "expired"])
def test_each_late_upload_chunk_reclamps_socket_write_timeout(monkeypatch, late_delay):
    clock, writes = [100.0], []
    monkeypatch.setattr(provider_transport, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class Socket:
        timeout = 10.0

        def settimeout(self, value):
            self.timeout = value

        def sendall(self, value):
            writes.append((value, self.timeout))
            if value == b"late":
                # A blocked native sendall can wait for its configured timeout.
                clock[0] += self.timeout
                raise TimeoutError("Synthetic blocked write")

    connection = provider_transport._connection(
        provider_transport._HTTPConnection, 110.0, 120, "synthetic.invalid"
    )
    connection.sock = Socket()

    def chunks():
        clock[0] += 2.0
        yield b"first"
        clock[0] += late_delay
        yield b"late"

    with pytest.raises(TimeoutError):
        connection.request("POST", "/synthetic", body=chunks(), headers={"Content-Length": "9"})
    assert writes[0][0].startswith(b"POST ") and writes[0][1] == 10.0
    assert writes[1] == (b"first", 8.0)
    if late_delay == 6.0:
        assert writes[2] == (b"late", 2.0)
        assert clock[0] == 110.0
    else:
        assert len(writes) == 2, "Expired chunk must never reach sendall"


def test_late_upload_send_timeout_remains_unknown_without_retry(monkeypatch):
    clock, writes, connects = [100.0], [], []
    monkeypatch.setattr(provider_transport, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(artifact_http, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class Socket:
        timeout = 10.0

        def settimeout(self, value):
            self.timeout = value

        def sendall(self, value):
            writes.append((value, self.timeout))
            if value == b"synthetic-body":
                clock[0] += self.timeout
                raise TimeoutError("Synthetic blocked upload")

        def close(self):
            pass

    def connect(connection):
        connects.append(connection)
        connection.sock = Socket()

    # Replace only native connection establishment; urllib still builds and
    # sends the real request through the deadline-aware HTTPConnection.
    monkeypatch.setattr(provider_transport.HTTPConnection, "connect", connect)

    class Source(io.BytesIO):
        def read(self, amount):
            clock[0] = 108.0
            return super().read(amount)

    snapshot = SimpleNamespace(stream=Source(b"synthetic-body"), size=len(b"synthetic-body"))
    with pytest.raises(PlayError, match="outcome unknown"):
        artifact_http.upload(
            {"url": "http://127.0.0.1/synthetic", "mime_type": "application/octet-stream"},
            "synthetic-token",
            snapshot,
            deadline=110.0,
        )
    assert len(connects) == 1 and len(writes) == 2
    assert writes[1] == (b"synthetic-body", 2.0)
    assert clock[0] == 110.0


def test_media_mutation_slow_response_is_unknown_and_never_retried(monkeypatch):
    monkeypatch.setattr(artifact_http, "SOCKET_TIMEOUT", 0.2)
    prefix = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
    with upstream(prefix, b"x" * 100) as (url, requests):
        started = time.monotonic()
        with pytest.raises(PlayError, match="outcome unknown"):
            artifact_http.upload({"url": url}, "synthetic-token", body={}, deadline=started + 0.35)
        assert time.monotonic() - started < 1.2
        assert len(requests) == 1


def test_media_download_deadline_closes_partial_destination(monkeypatch):
    monkeypatch.setattr(artifact_http, "SOCKET_TIMEOUT", 0.2)

    class Sink(io.BytesIO):
        cap = 4096

        @property
        def size(self):
            return self.tell()

        def publish(self):
            pytest.fail("Timed-out download must not publish")

    destination = Sink()
    prefix = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: 100\r\n\r\n"
    )
    with upstream(prefix, b"x" * 100) as (url, requests):
        started = time.monotonic()
        with pytest.raises(PlayError, match="download failed"):
            artifact_http.download(
                {"url": url}, "synthetic-token", destination, deadline=started + 0.35
            )
        assert time.monotonic() - started < 1.2
        assert destination.closed and len(requests) == 1


def signing_intent():
    return signing.request_spec(
        signing.PREFIX + "enrollApp",
        {"name": "com.example.app"},
        {
            "enrollExistingApp": {
                "cloudKmsKey": {
                    "cryptoKeyVersionResource": "projects/synthetic/locations/global/keyRings/test/cryptoKeys/signing/cryptoKeyVersions/1"
                }
            }
        },
    )


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_signing_slow_response_uses_original_deadline_and_unknown_outcome(monkeypatch, phase):
    payload = json.dumps({"signingCertificate": {"certificateHashSha256": "AB" * 32}}).encode()
    spec = signing_intent()
    framing = f"Content-Length: {len(payload)}\r\n\r\n".encode()
    if phase == "headers":
        prefix = b"HTTP/1.1 200 OK\r\nX-Slow: "
        trickle = b"x" * 30 + b"\r\n" + framing + payload
    else:
        prefix = b"HTTP/1.1 200 OK\r\n" + framing
        trickle = payload
    original = urllib.request.Request
    with upstream(prefix, trickle) as (url, requests):

        def local_request(provider_url, **kwargs):
            assert provider_url == (
                "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
                "com.example.app/appSigning:enrollApp"
            )
            # Redirect only the synthetic test's socket destination, after
            # verifying execute constructed the exact fixed signing route.
            return original(url, **kwargs)

        monkeypatch.setattr(urllib.request, "Request", local_request)
        authorized = Mock()
        started = time.monotonic()
        with pytest.raises(PlayError, match="Signing outcome unknown"):
            signing.execute(
                spec,
                "synthetic-signing-token",
                deadline=started + 0.35,
                _execution_authorization=authorized,
            )
        assert time.monotonic() - started < 1.2
        assert len(requests) == 1
        authorized.assert_called_once_with()


@pytest.mark.parametrize("failure", ["expired", "cancelled"])
def test_signing_final_guard_stays_outside_unknown_outcome_handling(monkeypatch, failure):
    clock, built = [100.0], []
    monkeypatch.setattr(signing, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original, opener = urllib.request.Request, Mock()
    monkeypatch.setattr(signing, "bounded_opener", Mock(return_value=opener))

    def construct(*args, **kwargs):
        request = original(*args, **kwargs)
        built.append(request)
        clock[0] = 102.0
        return request

    def authorize():
        if failure == "cancelled":
            raise PlayError("Synthetic cancellation before dispatch")

    monkeypatch.setattr(urllib.request, "Request", construct)
    with pytest.raises(PlayError, match="before dispatch") as error:
        signing.execute(
            signing_intent(), "synthetic-token", deadline=101.0, _execution_authorization=authorize
        )
    assert len(built) == 1 and "outcome unknown" not in str(error.value)
    opener.open.assert_not_called()


APP = "com.example.app"
EDIT = {"packageName": APP, "editId": "synthetic-edit"}
WRITES = [
    (edit_lifecycle_http.execute, ("create", {"packageName": APP}, "synthetic-token")),
    (
        publishing_http.listing_patch,
        (
            f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{APP}/edits/synthetic-edit/listings/en-US",
            "synthetic-token",
            {"title": "Synthetic"},
        ),
    ),
    (track_staging_http.execute, ({**EDIT, "track": "production"}, [], "synthetic-token")),
    (
        edit_writes_http.execute,
        (
            "androidpublisher.edits.details.patch",
            EDIT,
            {"contactEmail": "x@example.test"},
            "synthetic-token",
        ),
    ),
    (
        monetization_http.execute,
        (
            "androidpublisher.monetization.subscriptions.delete",
            {"packageName": APP, "productId": "premium"},
            None,
            "synthetic-token",
        ),
    ),
    (
        support_http.execute,
        (
            "androidpublisher.reviews.reply",
            {"packageName": APP, "reviewId": "review"},
            {"replyText": "Thanks"},
            "synthetic-token",
        ),
    ),
    (
        purchase_http.execute,
        (
            "androidpublisher.purchases.products.acknowledge",
            {"packageName": APP, "productId": "coins", "token": "synthetic-purchase"},
            {},
            "synthetic-token",
        ),
    ),
]


@pytest.mark.parametrize(
    "execute,args", WRITES, ids=lambda value: getattr(value, "__module__", None)
)
@pytest.mark.parametrize("failure", ["cancelled", "expired"])
def test_all_legacy_writes_guard_after_request_construction(monkeypatch, execute, args, failure):
    clock, built = [100.0], []
    monkeypatch.setattr(provider_transport, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = urllib.request.Request
    opener = Mock(handlers=[])
    monkeypatch.setattr(urllib.request, "build_opener", Mock(return_value=opener))

    def construct(*args, **kwargs):
        request = original(*args, **kwargs)
        built.append(request)
        clock[0] = 102.0
        return request

    def authorize():
        if failure == "cancelled" and built:
            raise PlayError("Synthetic cancellation before dispatch")

    monkeypatch.setattr(urllib.request, "Request", construct)
    with pytest.raises(PlayError, match="before dispatch") as error:
        execute(*args, deadline=101.0, _execution_authorization=authorize)
    assert len(built) == 1
    assert "outcome unknown" not in str(error.value)
    opener.open.assert_not_called()
