"""Reference byte-transfer client sharing its caller's authenticated MCP connection.

No Google credentials, redirects, automatic transfer retries or token persistence.
Local paths remain client-side and are never included in MCP tool arguments.
"""

import asyncio
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import httpx2

CHUNK = 1024 * 1024
HANDLE = re.compile(r"transfer_[A-Za-z0-9_-]{43}\Z")


class TransferClientError(Exception):
    """Safe public explanation; never include transport exceptions or tokens."""


def server_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise TransferClientError("Server must be an HTTPS origin without a path or credentials.")
    return value.rstrip("/")


def transfer_url(origin, status, expected):
    origin = server_origin(origin)
    if not isinstance(expected, str) or not HANDLE.fullmatch(expected):
        raise TransferClientError("Invalid opaque transfer handle.")
    path = f"/transfers/{expected}/content"
    if status.get("transfer_id") != expected or status.get("content_path") != path:
        raise TransferClientError("Transfer destination does not match its exact opaque handle.")
    return origin + path


def safe_metadata(value):
    """Only transfer/preparation controls; never dump provider payloads or secrets."""
    allowed = {
        "transfer_id",
        "state",
        "direction",
        "size_bytes",
        "maximum_bytes",
        "mime_type",
        "expires_in_seconds",
        "sha256",
        "preparation_id",
        "operation_id",
        "method",
        "package_name",
        "expires_at",
        "executed",
        "mutation_succeeded",
        "readback_succeeded",
        "committed",
        "publication_verified",
        "cleanup_warning",
        "observation_error",
        "baseline_available",
        "baseline_observation_available",
        "readback_available",
        "note",
        "status",
        "confirmation",
        "confirmation_text",
    }
    result = {
        key: item
        for key, item in value.items()
        if key in allowed and isinstance(item, (str, int, float, bool, type(None)))
    }
    # These are preparation intent/effects, never provider read responses.
    for key in ("proposed_request", "effects", "file", "source_file"):
        if key in value:
            result[key] = redact_preview(value[key])
    return result


def redact_preview(value):
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if any(
                    part in key.lower() for part in ("token", "secret", "authorization", "password")
                )
                else redact_preview(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_preview(item) for item in value]
    return value


async def tool(session, name, arguments):
    result = await session.call_tool(name, arguments)
    if result.is_error:
        raise TransferClientError(
            "MCP operation failed or was rejected. A write may have occurred; reconcile its outcome before retrying. No automatic retry was made."
        )
    value = result.structured_content
    if value is None:
        try:
            value = json.loads(result.content[0].text)
        except (AttributeError, IndexError, ValueError, TypeError):
            raise TransferClientError("MCP operation returned an unsupported result.") from None
    if not isinstance(value, dict):
        raise TransferClientError("MCP operation returned an unsupported result.")
    return value


class TransferClient:
    def __init__(self, origin, session, storage, *, transport=None):
        self.origin, self.session, self.storage = server_origin(origin), session, storage
        self.transport = transport

    async def _headers(self):
        # Refresh through the same SDK-managed MCP OAuth connection before
        # taking its access token. Raw HTTP must not replay streamed bodies.
        await tool(self.session, "get_connection_capabilities", {})
        tokens = await self.storage.get_tokens()
        if tokens is None or tokens.token_type.lower() != "bearer":
            raise TransferClientError("The current MCP connection has no bearer authorization.")
        return {"Authorization": "Bearer " + tokens.access_token}

    def _http(self):
        return httpx2.AsyncClient(
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx2.Timeout(30, read=60, write=60),
        )

    async def upload(self, *, source, method, parameters, mime_type, body=None):
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_uid != os.getuid()
                or initial.st_mode & 0o022
            ):
                raise TransferClientError(
                    "Source must be an owned regular file without group/other write access."
                )
            digest = hashlib.sha256()
            while chunk := await asyncio.to_thread(stream.read, CHUNK):
                digest.update(chunk)
            if initial.st_size <= 0:
                raise TransferClientError("An upload must contain bytes.")
            expected_hash = digest.hexdigest()
            arguments = {
                "method": method,
                "parameters": parameters,
                "mime_type": mime_type,
                "size_bytes": initial.st_size,
                "sha256": expected_hash,
            }
            if body is not None:
                arguments["body"] = body
            status = await tool(self.session, "begin_upload_transfer", arguments)
            handle = status.get("transfer_id")
            url = transfer_url(self.origin, status, handle)
            headers = await self._headers()
            headers.update({"Content-Type": mime_type, "Content-Length": str(initial.st_size)})
            stream.seek(0)

            async def chunks():
                sent, actual = 0, hashlib.sha256()
                while chunk := await asyncio.to_thread(stream.read, CHUNK):
                    sent += len(chunk)
                    if sent > initial.st_size:
                        raise TransferClientError("Source changed during upload.")
                    actual.update(chunk)
                    yield chunk
                current = os.fstat(stream.fileno())
                if (
                    sent != initial.st_size
                    or actual.hexdigest() != expected_hash
                    or current.st_mtime_ns != initial.st_mtime_ns
                    or current.st_size != initial.st_size
                ):
                    raise TransferClientError("Source changed during upload.")

            try:
                async with self._http() as client:
                    async with client.stream(
                        "PUT", url, headers=headers, content=chunks()
                    ) as response:
                        if response.status_code != 200:
                            raise TransferClientError(
                                f"Upload failed for {handle}; inspect its status before reserving another upload."
                            )
                        # Ignore response bytes; status is fetched through MCP.
            except (httpx2.HTTPError, OSError):
                raise TransferClientError(
                    f"Upload response was lost or interrupted for {handle}. Inspect transfer status; do not blindly retry."
                ) from None
            return safe_metadata(
                await tool(self.session, "get_transfer_status", {"transfer_id": handle})
            )

    async def download(self, *, transfer_id, destination):
        status = await tool(self.session, "get_transfer_status", {"transfer_id": transfer_id})
        url = transfer_url(self.origin, status, transfer_id)
        size, expected = status.get("size_bytes"), status.get("sha256")
        if (
            status.get("state") != "DOWNLOAD_READY"
            or type(size) is not int
            or size < 0
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            raise TransferClientError("Download is not ready with a verified size and digest.")
        headers = await self._headers()
        path = Path(destination)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(directory)
            if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise TransferClientError(
                    "Download destination directory must be private (0700) and owned by you."
                )
            fd = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            identity = os.fstat(fd)
        except BaseException:
            os.close(directory)
            raise
        complete = False
        try:
            with os.fdopen(fd, "wb") as stream:
                digest, total = hashlib.sha256(), 0
                async with self._http() as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        if (
                            response.status_code != 200
                            or response.headers.get("content-encoding", "identity") != "identity"
                        ):
                            raise TransferClientError(
                                "Download was rejected; no redirect was followed."
                            )
                        async for chunk in response.aiter_raw(CHUNK):
                            total += len(chunk)
                            if total > size:
                                raise TransferClientError("Download exceeded its verified size.")
                            digest.update(chunk)
                            await asyncio.to_thread(stream.write, chunk)
                if total != size or digest.hexdigest() != expected:
                    raise TransferClientError("Download size or digest did not match.")
                stream.flush()
                os.fsync(stream.fileno())
            complete = True
            return {"state": "saved", "size_bytes": size, "sha256": expected}
        except (httpx2.HTTPError, OSError):
            raise TransferClientError(
                "Download was interrupted; incomplete local output was removed."
            ) from None
        finally:
            try:
                if not complete:
                    try:
                        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                            os.unlink(path.name, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(directory)
