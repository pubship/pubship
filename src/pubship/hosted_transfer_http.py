"""Authenticated byte routes: no request-body access before current dimensional authority."""

import re
import time

import anyio
from starlette.responses import JSONResponse, Response

from .errors import PlayError
from .hosted_pages import SECURE_HEADERS
from .hosted_transfers import CHUNK_BYTES, TRANSFER_ID

CONTENT_ROUTE = re.compile(r"/transfers/[^/]+/content\Z")


def is_transfer_route(scope):
    return bool(CONTENT_ROUTE.fullmatch(scope.get("path", "")))


def authorize_purpose(provider, principal, purpose):
    if purpose.store is not None:
        return provider.authorize_appstore_current(
            principal, purpose.capability, purpose.store, purpose.target
        )
    return provider.authorize_current(principal, purpose.capability, purpose.package)


def _headers(request, issuer):
    result = {}
    guarded = {
        b"authorization",
        b"content-length",
        b"content-type",
        b"content-encoding",
        b"transfer-encoding",
        b"origin",
        b"range",
        b"sec-fetch-site",
    }
    for key, value in request.scope.get("headers", []):
        key = key.lower()
        if key in guarded:
            if key in result:
                raise PlayError("Duplicate transfer header.")
            result[key] = value
    if request.scope.get("query_string"):
        raise PlayError("Transfer URLs do not accept query parameters.")
    if (
        result.get(b"origin") not in {None, issuer.encode()}
        or result.get(b"sec-fetch-site") == b"cross-site"
    ):
        raise PlayError("Cross-origin transfer requests are not permitted.")
    if b"range" in result:
        raise PlayError("Transfer range requests are not supported.")
    if result.get(b"content-encoding") not in {None, b"identity"}:
        raise PlayError("Transfer content encoding is not supported.")
    if result.get(b"transfer-encoding") not in {None, b"chunked"} or (
        b"transfer-encoding" in result and b"content-length" in result
    ):
        raise PlayError("Conflicting transfer framing headers.")
    authorization = result.get(b"authorization", b"")
    if not re.fullmatch(rb"Bearer [A-Za-z0-9._~-]{1,4096}", authorization):
        raise PlayError("Bearer authorization is required for transfer content.")
    return result, authorization[7:].decode("ascii")


class _DownloadResponse(Response):
    """Bounded delivery; a late failure terminates the declared-length response."""

    def __init__(self, lease, limits, mime_type):
        self.lease, self.limits = lease, limits
        super().__init__(
            status_code=200,
            headers={
                **SECURE_HEADERS,
                "Content-Type": mime_type,
                "Content-Length": str(lease.size),
                "Content-Disposition": 'attachment; filename="artifact.bin"',
                "Accept-Ranges": "none",
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            remaining = min(
                self.lease.deadline - time.monotonic(), self.limits.stream_timeout_seconds
            )
            if remaining <= 0:
                raise PlayError("Transfer expired before delivery.")
            with anyio.fail_after(remaining):
                failure = None
                async with anyio.create_task_group() as tasks:

                    async def disconnected():
                        # This response is constructed only after authorization.
                        # Uvicorn send() can silently discard bytes after a peer
                        # disconnects; only receive() reliably signals it.
                        while True:
                            if (await receive())["type"] == "http.disconnect":
                                tasks.cancel_scope.cancel()
                                return

                    tasks.start_soon(disconnected)
                    try:
                        await self._stream(send)
                    except Exception as error:
                        # Preserve the original streaming exception, rather than
                        # replacing it with the task group's ExceptionGroup.
                        failure = error
                    finally:
                        tasks.cancel_scope.cancel()
                if failure is not None:
                    raise failure
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.lease.close)

    async def _stream(self, send):
        first = await anyio.to_thread.run_sync(self.lease.read, CHUNK_BYTES)
        with anyio.fail_after(self.limits.idle_timeout_seconds):
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
        chunk = first
        while chunk:
            await anyio.to_thread.run_sync(self.lease.validate)
            with anyio.fail_after(self.limits.idle_timeout_seconds):
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            chunk = await anyio.to_thread.run_sync(self.lease.read, CHUNK_BYTES)
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class TransferHTTP:
    def __init__(self, provider, store, issuer):
        self.provider, self.store, self.issuer = provider, store, issuer

    async def content(self, request):
        lease = None
        if request.method not in {"GET", "PUT"}:
            return JSONResponse(
                {"error": "method_not_allowed"},
                status_code=405,
                headers={**SECURE_HEADERS, "Allow": "GET, PUT"},
            )
        try:
            headers, token = _headers(request, self.issuer)
            transfer_id = request.path_params.get("transfer_id")
            if not isinstance(transfer_id, str) or not TRANSFER_ID.fullmatch(transfer_id):
                raise PlayError("Invalid transfer route.")
            principal = await self.provider.load_access_token(token)
            if principal is None:
                return JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                    headers={**SECURE_HEADERS, "WWW-Authenticate": "Bearer"},
                )
            owner = (principal.subject, principal.client_id)
            # Both lookups occur before request.receive(), including foreign IDs.
            purpose = await anyio.to_thread.run_sync(self.store.purpose, owner, transfer_id)

            def authorize():
                return authorize_purpose(self.provider, principal, purpose)

            await anyio.to_thread.run_sync(authorize)
            status = await anyio.to_thread.run_sync(self.store.status, owner, transfer_id)
            if request.method == "GET":
                if status["direction"] != "download":
                    raise PlayError("This transfer is not a download.")
                lease = await anyio.to_thread.run_sync(
                    lambda: self.store.open_download(owner, transfer_id, authorize=authorize)
                )
                return _DownloadResponse(lease, self.store.limits, purpose.mime_type)
            if request.method != "PUT" or status["direction"] != "upload":
                raise PlayError("Unsupported transfer direction.")
            if headers.get(b"content-type") != purpose.mime_type.encode():
                raise PlayError("Transfer MIME type must match its reservation.")
            length = headers.get(b"content-length")
            if length is not None and (
                not re.fullmatch(rb"[1-9][0-9]{0,18}", length)
                or int(length) != status["maximum_bytes"]
            ):
                raise PlayError("Transfer length must match its reservation.")
            lease = await anyio.to_thread.run_sync(
                lambda: self.store.begin_receive(owner, transfer_id, authorize=authorize)
            )
            remaining = min(
                lease.deadline - time.monotonic(), self.store.limits.stream_timeout_seconds
            )
            if remaining <= 0:
                raise PlayError("Transfer expired before receiving bytes.")
            with anyio.fail_after(remaining):
                while True:
                    # No grant lock spans await. Each read/write validates current
                    # authority and the original lease deadline independently.
                    await anyio.to_thread.run_sync(authorize)
                    if time.monotonic() >= lease.deadline:
                        raise PlayError("Transfer expired while receiving bytes.")
                    with anyio.fail_after(self.store.limits.idle_timeout_seconds):
                        event = await request.receive()
                    if event["type"] == "http.disconnect":
                        raise PlayError("Transfer interrupted; reserve a new upload.")
                    if event["type"] != "http.request":
                        raise PlayError("Unexpected transfer body event.")
                    body = event.get("body", b"")
                    if (
                        not isinstance(body, bytes)
                        or len(body) > status["maximum_bytes"] - lease.size
                    ):
                        raise PlayError("Transfer exceeds its reserved byte length.")
                    for offset in range(0, len(body), CHUNK_BYTES):
                        await anyio.to_thread.run_sync(
                            lease.write, body[offset : offset + CHUNK_BYTES]
                        )
                    if not event.get("more_body", False):
                        break
                result = await anyio.to_thread.run_sync(lease.seal)
            lease = None
            return JSONResponse(result, headers=SECURE_HEADERS)
        except (PlayError, TimeoutError, OSError, ValueError):
            return JSONResponse(
                {
                    "error": "transfer_rejected",
                    "message": "Transfer could not be authorized or completed. Inspect status before starting again.",
                },
                status_code=400,
                headers=SECURE_HEADERS,
            )
        finally:
            # A successful GET owns cleanup in its response, and a sealed PUT
            # retains its immutable READY record for a subsequent preparation.
            if lease is not None and request.method != "GET":
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lease.dispose)
