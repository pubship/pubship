"""Byte-route auth before ASGI receive, bounded upload and repeat download delivery."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request
from test_hosted_020_transfers import DATA, OWNER, PURPOSE, ready, reserve
from test_hosted_020_transfers import store as store  # noqa: F401

from pubship.errors import PlayError
from pubship.hosted import RequestLimits
from pubship.hosted_transfer_http import TransferHTTP
from pubship.hosted_transfers import TransferPurpose

ISSUER = "https://mcp.example.test"


class Provider:
    def __init__(self):
        self.principal = SimpleNamespace(subject=OWNER[0], client_id=OWNER[1])
        self.allowed = True
        self.calls = []

    async def load_access_token(self, token):
        return self.principal if token == "valid-token" else None

    def authorize_current(self, principal, capability=None, package=None):
        self.calls.append((capability, package))
        if not self.allowed:
            raise PlayError("Denied synthetic authority")

    def authorize_appstore_current(self, principal, capability, store, target=None):
        self.calls.append((capability, store, target))
        if not self.allowed:
            raise PlayError("Denied store pair")


def scope(handle, *, method="PUT", headers=None, query=b""):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": f"/transfers/{handle}/content",
        "raw_path": f"/transfers/{handle}/content".encode(),
        "root_path": "",
        "query_string": query,
        "headers": headers
        if headers is not None
        else [
            (b"authorization", b"Bearer valid-token"),
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(len(DATA)).encode()),
        ],
        "client": ("127.0.0.1", 4000),
        "server": ("mcp.example.test", 443),
        "path_params": {"transfer_id": handle},
    }


def call(store, provider, request_scope, events=None, *, before_receive=None):
    reads, sent = [], []
    queue = list(events or [{"type": "http.request", "body": DATA, "more_body": False}])

    async def receive():
        if not queue:
            await asyncio.Event().wait()
        reads.append(1)
        if before_receive:
            before_receive(len(reads))
        return queue.pop(0)

    async def send(event):
        sent.append(event)

    async def app(s, r, send):
        response = await TransferHTTP(provider, store, ISSUER).content(Request(s, r))
        await response(s, r, send)

    async def run():
        await RequestLimits(app, ISSUER + "/mcp")(request_scope, receive, send)

    asyncio.run(run())
    return reads, sent


def test_authenticated_put_seals_only_exact_bytes(store):
    provider = Provider()
    handle = reserve(store)
    reads, sent = call(store, provider, scope(handle))
    assert reads == [1] and sent[0]["status"] == 200
    assert store.status(OWNER, handle)["state"] == "READY"
    assert len(provider.calls) >= 4
    lease = store.claim_upload(OWNER, PURPOSE, handle)
    lease.stream.seek(0)
    assert lease.stream.read() == DATA
    lease.dispose()


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "bad-token",
        "duplicate-auth",
        "foreign-client",
        "foreign-subject",
        "revoked",
        "origin",
        "query",
        "range",
        "encoding",
        "mime",
        "length",
        "duplicate-length",
        "conflicting-framing",
        "malformed-id",
        "expired",
        "ready",
    ],
)
def test_rejected_route_reads_zero_body_events_and_does_not_consume_victim(store, problem):
    provider = Provider()
    handle = ready(store) if problem == "ready" else reserve(store)
    request_scope = scope(handle)
    headers = request_scope["headers"]
    if problem == "missing":
        headers.pop(0)
    elif problem == "bad-token":
        headers[0] = (b"authorization", b"Bearer bad-token")
    elif problem == "duplicate-auth":
        headers.append((b"authorization", b"Bearer valid-token"))
    elif problem == "foreign-client":
        provider.principal.client_id = "other-client"
    elif problem == "foreign-subject":
        provider.principal.subject = "other-connection"
    elif problem == "revoked":
        provider.allowed = False
    elif problem == "origin":
        headers.append((b"origin", b"https://attacker.example"))
    elif problem == "query":
        request_scope["query_string"] = b"access_token=must-not-use"
    elif problem == "range":
        headers.append((b"range", b"bytes=0-1"))
    elif problem == "encoding":
        headers.append((b"content-encoding", b"gzip"))
    elif problem == "mime":
        headers[1] = (b"content-type", b"image/png")
    elif problem == "length":
        headers[2] = (b"content-length", b"999")
    elif problem == "duplicate-length":
        headers.append(headers[2])
    elif problem == "conflicting-framing":
        headers.append((b"transfer-encoding", b"chunked"))
    elif problem == "malformed-id":
        request_scope = scope("bad-handle")
    elif problem == "expired":
        store._records[handle].deadline = time.monotonic()
    reads, sent = call(store, provider, request_scope)
    assert not reads and sent[0]["status"] in {400, 401}
    assert handle in store._records


@pytest.mark.parametrize("body", [b"", DATA[:-1], DATA + b"!", b"x" * len(DATA)])
def test_stream_length_and_hash_failures_dispose_and_release_capacity(store, body):
    handle = reserve(store)
    reads, sent = call(
        store,
        Provider(),
        scope(handle),
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert reads == [1] and sent[0]["status"] == 400
    assert handle not in store._records


def test_chunked_without_content_length_is_bounded_by_reservation(store):
    handle = reserve(store)
    request_scope = scope(handle)
    request_scope["headers"] = request_scope["headers"][:2] + [(b"transfer-encoding", b"chunked")]
    events = [
        {"type": "http.request", "body": DATA[:4], "more_body": True},
        {"type": "http.request", "body": DATA[4:], "more_body": False},
    ]
    reads, sent = call(store, Provider(), request_scope, events)
    assert len(reads) == 2 and sent[0]["status"] == 200
    assert store.status(OWNER, handle)["state"] == "READY"


@pytest.mark.parametrize("change", ["revoke", "expire", "disconnect"])
def test_authority_loss_between_receive_and_write_stops_and_disposes(store, change):
    handle = reserve(store)
    provider = Provider()

    def change_authority(count):
        if change == "revoke":
            provider.allowed = False
        elif change == "expire":
            store._records[handle].deadline = time.monotonic()
        else:
            store.invalidate(OWNER[0])

    reads, sent = call(store, provider, scope(handle), before_receive=change_authority)
    assert len(reads) == 1 and sent[0]["status"] == 400
    assert handle not in store._records


def test_disconnect_body_event_disposes_upload(store):
    handle = reserve(store)
    reads, sent = call(store, Provider(), scope(handle), [{"type": "http.disconnect"}])
    assert len(reads) == 1 and sent[0]["status"] == 400
    assert handle not in store._records


def test_download_uses_current_authority_and_bounded_repeat_delivery(store):
    purpose = TransferPurpose(
        "androidpublisher.generatedapks.download",
        {"packageName": PURPOSE.package},
        PURPOSE.mime_type,
        "play:artifact-downloads",
        package=PURPOSE.package,
    )
    destination = store.reserve_download(OWNER, purpose, 4096, time.monotonic() + 30)
    with destination:
        destination.write(DATA)
        handle = destination.publish()["transfer_id"]
    provider = Provider()
    for _ in range(2):
        reads, sent = call(store, provider, scope(handle, method="GET"))
        assert sent[0]["status"] == 200
        assert b"".join(event.get("body", b"") for event in sent) == DATA
    assert store.status(OWNER, handle)["state"] == "DOWNLOAD_READY"


def test_store_pair_authority_is_rechecked_for_byte_route(store):
    purpose = TransferPurpose(
        "androidpublisher.appstoreappsreview.uploadapk",
        {"appStorePackageName": "com.example.store", "packageName": "com.example.target"},
        PURPOSE.mime_type,
        "play:appstore-media",
        store="com.example.store",
        target="com.example.target",
    )
    handle = reserve(store, purpose=purpose)
    provider = Provider()
    reads, sent = call(store, provider, scope(handle))
    assert reads == [1] and sent[0]["status"] == 200
    assert provider.calls and all(
        call == ("play:appstore-media", "com.example.store", "com.example.target")
        for call in provider.calls
    )


def test_non_transfer_request_limit_still_applies_before_inner_app():
    inner = Mock()
    scope_value = scope("valid-looking")
    scope_value["path"] = "/mcp"
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"x" * (256 * 1024 + 1)}

    async def send(event):
        sent.append(event)

    asyncio.run(RequestLimits(inner, ISSUER + "/mcp")(scope_value, receive, send))
    assert sent[0]["status"] == 413
    inner.assert_not_called()


@pytest.mark.parametrize("method", ["HEAD", "POST", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("malformed", [False, True])
def test_unsupported_transfer_methods_never_receive_body(store, method, malformed):
    handle = "malformed" if malformed else reserve(store)
    reads, sent = call(store, Provider(), scope(handle, method=method))
    assert reads == [] and sent[0]["status"] == 405


@pytest.mark.parametrize("change", ["revoke", "invalidate", "expire"])
def test_revocation_during_header_send_cannot_deliver_buffered_bytes(store, change):
    destination = store.reserve_download(OWNER, PURPOSE, 4096, time.monotonic() + 30)
    with destination:
        destination.write(DATA)
        handle = destination.publish()["transfer_id"]
    provider, sent = Provider(), []

    async def receive():
        await asyncio.Event().wait()

    async def send(event):
        sent.append(event)
        if event["type"] == "http.response.start":
            if change == "revoke":
                provider.allowed = False
            elif change == "invalidate":
                store.invalidate(OWNER[0])
            else:
                store._records[handle].deadline = time.monotonic()

    async def run():
        value = scope(handle, method="GET")
        response = await TransferHTTP(provider, store, ISSUER).content(Request(value, receive))
        with pytest.raises(PlayError):
            await response(value, receive, send)

    asyncio.run(run())
    assert all(not event.get("body") for event in sent)
    assert handle not in store._records or not store._records[handle].active
