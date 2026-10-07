"""Single-attempt, bounded streaming transport on fixed Google artifact routes."""

import json
import time
import urllib.error
import urllib.request
from http.client import HTTPException

from .artifact_files import CHUNK_BYTES
from .auth import validate_token
from .errors import PlayError
from .http import MAX_BYTES, NoRedirect, finite_float
from .provider_transport import bounded_opener, read_chunk

SOCKET_TIMEOUT = 120
TRANSFER_SECONDS = 3600
UNKNOWN_OUTCOME = (
    "Artifact upload outcome unknown; reconcile the caller-owned edit before preparing again. "
    "Do not automatically retry. This operation ID has been consumed."
)
DOWNLOAD_ERROR = (
    "Artifact download failed; no completed file was published. No Google mutation was attempted."
)


class _Body:
    def __init__(self, snapshot, deadline, _execution_authorization=None):
        self.snapshot, self.deadline = snapshot, deadline
        self._execution_authorization = _execution_authorization
        self.sent = 0
        snapshot.stream.seek(0)

    def read(self, amount=-1):
        _check(self.deadline, self._execution_authorization, UNKNOWN_OUTCOME)
        data = self.snapshot.stream.read(min(CHUNK_BYTES, amount) if amount > 0 else CHUNK_BYTES)
        _check(self.deadline, self._execution_authorization, UNKNOWN_OUTCOME)
        self.sent += len(data)
        if self.sent > self.snapshot.size or (not data and self.sent != self.snapshot.size):
            raise PlayError(UNKNOWN_OUTCOME)
        return data


def _check(deadline, authorization, error):
    if authorization is not None:
        authorization()
    if time.monotonic() >= deadline:
        raise PlayError(error)


def upload(spec, token, snapshot=None, body=None, *, deadline=None, _execution_authorization=None):
    hosted = _execution_authorization is not None
    deadline = (
        min(time.monotonic() + TRANSFER_SECONDS, deadline)
        if deadline is not None
        else time.monotonic() + TRANSFER_SECONDS
    )
    _check(deadline, _execution_authorization, UNKNOWN_OUTCOME)
    data = (
        _Body(snapshot, deadline, _execution_authorization)
        if snapshot is not None
        else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    )
    req = urllib.request.Request(
        spec["url"],
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + validate_token(token),
            "Accept": "application/json",
            "Content-Type": spec["mime_type"] if snapshot is not None else "application/json",
            "Content-Length": str(snapshot.size if snapshot is not None else len(data)),
        },
    )
    opener = bounded_opener(
        NoRedirect(), timeout=SOCKET_TIMEOUT, deadline=deadline, total_timeout=TRANSFER_SECONDS
    )
    # Admission failures before dispatch are definite failures, not unknown writes.
    _check(deadline, _execution_authorization, UNKNOWN_OUTCOME)
    try:
        with opener.open(req, timeout=SOCKET_TIMEOUT) as response:
            if not 200 <= response.status < 300:
                raise PlayError(UNKNOWN_OUTCOME)
            content = response.read(MAX_BYTES + 1)
        # A valid completed write remains evidence even if authority expired while
        # its response arrived. Subsequent engine readback is independently guarded.
        if (not hosted and time.monotonic() >= deadline) or len(content) > MAX_BYTES:
            raise PlayError(UNKNOWN_OUTCOME)
        return json.loads(content, parse_float=finite_float, parse_constant=finite_float)
    except urllib.error.HTTPError as error:
        error.close()
        raise PlayError(f"Google API HTTP {error.code}. {UNKNOWN_OUTCOME}") from None
    except (
        PlayError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        HTTPException,
        RecursionError,
    ):
        raise PlayError(UNKNOWN_OUTCOME) from None


def download(spec, token, destination, *, deadline=None, _execution_authorization=None):
    deadline = (
        min(time.monotonic() + TRANSFER_SECONDS, deadline)
        if deadline is not None
        else time.monotonic() + TRANSFER_SECONDS
    )
    req = urllib.request.Request(
        spec["url"],
        method="GET",
        headers={
            "Authorization": "Bearer " + validate_token(token),
            "Accept": "application/octet-stream",
        },
    )
    try:
        with destination:
            opener = bounded_opener(
                NoRedirect(),
                timeout=SOCKET_TIMEOUT,
                deadline=deadline,
                total_timeout=TRANSFER_SECONDS,
            )
            _check(deadline, _execution_authorization, DOWNLOAD_ERROR)
            with opener.open(req, timeout=SOCKET_TIMEOUT) as response:
                if response.status != 200:
                    raise PlayError(DOWNLOAD_ERROR)
                content_type = (
                    response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                )
                if (
                    content_type.startswith("text/")
                    or content_type
                    in {"application/json", "application/problem+json", "application/xhtml+xml"}
                    or content_type.endswith("+json")
                ):
                    raise PlayError(DOWNLOAD_ERROR)
                length = response.headers.get("Content-Length")
                if length is not None:
                    if (
                        not length.isascii()
                        or not length.isdecimal()
                        or not 0 < int(length) <= destination.cap
                    ):
                        raise PlayError(DOWNLOAD_ERROR)
                    length = int(length)
                while True:
                    _check(deadline, _execution_authorization, DOWNLOAD_ERROR)
                    chunk = read_chunk(response, CHUNK_BYTES)
                    _check(deadline, _execution_authorization, DOWNLOAD_ERROR)
                    if not chunk:
                        break
                    if not destination.size and chunk.lstrip()[:1] in (b"{", b"[", b"<"):
                        raise PlayError(DOWNLOAD_ERROR)
                    destination.write(chunk)
                if time.monotonic() >= deadline or (
                    length is not None and destination.size != length
                ):
                    raise PlayError(DOWNLOAD_ERROR)
            _check(deadline, _execution_authorization, DOWNLOAD_ERROR)
            return destination.publish()
    except urllib.error.HTTPError as error:
        error.close()
        raise PlayError(f"Google API HTTP {error.code}. {DOWNLOAD_ERROR}") from None
    except (
        PlayError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        HTTPException,
        RecursionError,
    ):
        raise PlayError(DOWNLOAD_ERROR) from None
