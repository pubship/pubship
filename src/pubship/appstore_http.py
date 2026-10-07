"""Bounded single-attempt transport including streamed policy metadata plus media."""

import io
import json
import secrets
import time
import urllib.error
import urllib.request
from http.client import HTTPException

from .appstore_contracts import (
    POLICY,
    UPLOAD_METHODS,
    WRITE_METHODS,
    request_spec,
    validate_response,
)
from .artifact_files import CHUNK_BYTES
from .auth import validate_token
from .errors import PlayError
from .http import NoRedirect, finite_float
from .provider_transport import bounded_opener, dispatch_check

MAX_BYTES = 2 * 1024 * 1024
UNKNOWN_OUTCOME = "App-store mutation outcome unknown; reconcile with the provider before preparing again. Do not automatically retry. The operation ID is consumed."


class MediaBody:
    def __init__(self, spec, snapshot, *, deadline=None, _execution_authorization=None):
        self.deadline = (
            min(time.monotonic() + 3600, deadline)
            if deadline is not None
            else time.monotonic() + 3600
        )
        self._execution_authorization = _execution_authorization
        snapshot.stream.seek(0)
        self.content_type = spec["mime_type"]
        self.size = snapshot.size
        self.parts = [snapshot.stream]
        if spec["method"] == POLICY:
            boundary = "pubship_" + secrets.token_hex(32)
            metadata = json.dumps(spec["body"], allow_nan=False).encode()
            prefix = (
                (
                    "--" + boundary + "\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                ).encode()
                + metadata
                + (
                    "\r\n--" + boundary + "\r\nContent-Type: " + spec["mime_type"] + "\r\n\r\n"
                ).encode()
            )
            suffix = ("\r\n--" + boundary + "--\r\n").encode()
            self.parts = [io.BytesIO(prefix), snapshot.stream, io.BytesIO(suffix)]
            self.size += len(prefix) + len(suffix)
            self.content_type = "multipart/related; boundary=" + boundary
        self.sent = 0

    def read(self, amount=-1):
        if self._execution_authorization is not None:
            self._execution_authorization()
        if time.monotonic() >= self.deadline:
            raise PlayError(UNKNOWN_OUTCOME)
        amount = min(amount, CHUNK_BYTES) if amount > 0 else CHUNK_BYTES
        while self.parts:
            chunk = self.parts[0].read(amount)
            if self._execution_authorization is not None:
                self._execution_authorization()
            if time.monotonic() >= self.deadline:
                raise PlayError(UNKNOWN_OUTCOME)
            if chunk:
                self.sent += len(chunk)
                if self.sent > self.size:
                    raise PlayError(UNKNOWN_OUTCOME)
                return chunk
            self.parts.pop(0)
        if self.sent != self.size:
            raise PlayError(UNKNOWN_OUTCOME)
        return b""


def execute(spec, token, snapshot=None, *, deadline=None, _execution_authorization=None):
    # Rebuild from fixed contracts: a caller-supplied URL is never used.
    spec = request_spec(spec["method"], spec["parameters"], spec["body"], spec["mime_type"])
    mutation = spec["method"] in WRITE_METHODS
    media = spec["method"] in UPLOAD_METHODS
    if media != (snapshot is not None):
        raise PlayError("App-store uploads require exactly one prepared file snapshot.")
    headers = {"Authorization": "Bearer " + validate_token(token), "Accept": "application/json"}
    data = None
    if media:
        data = MediaBody(
            spec, snapshot, deadline=deadline, _execution_authorization=_execution_authorization
        )
        headers.update({"Content-Type": data.content_type, "Content-Length": str(data.size)})
    elif mutation:
        data = json.dumps(spec["body"], allow_nan=False).encode()
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
    request = urllib.request.Request(
        spec["url"], method=spec["http_method"], headers=headers, data=data
    )
    opener = bounded_opener(
        NoRedirect(),
        timeout=120 if media else 15,
        deadline=deadline,
        total_timeout=3600 if media else 15,
    )
    dispatch_check(deadline, _execution_authorization)
    try:
        with opener.open(request, timeout=120 if media else 15) as response:
            status = response.status
            content = response.read(MAX_BYTES + 1)
        if status not in {200, 201, 204} or len(content) > MAX_BYTES:
            raise ValueError("Invalid response")
        if media and _execution_authorization is None and time.monotonic() >= data.deadline:
            raise ValueError("Transfer deadline")
        if not mutation:
            if _execution_authorization is not None:
                _execution_authorization()
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError("Read deadline")
        if not content and mutation and not media:
            result = {}
        else:
            result = json.loads(content, parse_float=finite_float, parse_constant=finite_float)
        validate_response(spec, result)
        return result
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise PlayError(
            f"Google API HTTP {status}. "
            + (UNKNOWN_OUTCOME if mutation else "App-store read unavailable.")
        ) from None
    except (
        PlayError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        HTTPException,
        RecursionError,
    ):
        raise PlayError(
            UNKNOWN_OUTCOME if mutation else "App-store read returned invalid or unavailable data."
        ) from None
