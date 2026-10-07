"""Reference client never forwards auth to untrusted paths or replays bytes."""

import asyncio
import hashlib
import os
from types import SimpleNamespace

import httpx2
import pytest
from mcp.types import CallToolResult

from pubship.hosted_transfer_client import (
    TransferClient,
    TransferClientError,
    safe_metadata,
    transfer_url,
)

HANDLE = "transfer_" + "A" * 43
ORIGIN = "https://mcp.example.test"
DATA = b"synthetic file"


def status(**values):
    return {
        "transfer_id": HANDLE,
        "content_path": f"/transfers/{HANDLE}/content",
        "state": "DOWNLOAD_READY",
        "size_bytes": len(DATA),
        "sha256": hashlib.sha256(DATA).hexdigest(),
        **values,
    }


class Session:
    def __init__(self, value=None):
        self.value = value or status()
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return CallToolResult(content=[], isError=False, structuredContent=self.value)


class Storage:
    async def get_tokens(self):
        return SimpleNamespace(access_token="synthetic-token", token_type="Bearer")


def client(handler, session=None):
    return TransferClient(
        ORIGIN, session or Session(), Storage(), transport=httpx2.MockTransport(handler)
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.invalid/content",
        "//evil.invalid/content",
        "/mcp",
        f"/transfers/{HANDLE}/content?token=x",
        f"/transfers/transfer_{'B' * 43}/content",
        f"/transfers/{HANDLE}/content#x",
    ],
)
def test_exact_handle_path_before_any_bearer_attachment(path):
    with pytest.raises(TransferClientError):
        transfer_url(ORIGIN, status(content_path=path), HANDLE)


def test_upload_same_token_exact_bytes_and_no_local_path_argument(tmp_path):
    source = tmp_path / "app.apk"
    source.write_bytes(DATA)
    source.chmod(0o600)
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        assert await request.aread() == DATA
        return httpx2.Response(200, json={"ignored": "no credentials here"})

    session = Session(status(state="READY"))
    result = asyncio.run(
        client(handler, session).upload(
            source=source,
            method="example",
            parameters={"packageName": "com.example.app"},
            mime_type="application/octet-stream",
        )
    )
    assert result["state"] == "READY" and len(calls) == 1
    arguments = session.calls[0][1]
    assert "source" not in arguments and str(source) not in str(arguments)
    assert arguments["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert [name for name, _ in session.calls] == [
        "begin_upload_transfer",
        "get_connection_capabilities",
        "get_transfer_status",
    ]


@pytest.mark.parametrize("outcome", ["redirect", "lost"])
def test_upload_redirect_or_lost_response_is_never_retried(tmp_path, outcome):
    source = tmp_path / "source"
    source.write_bytes(DATA)
    calls = []

    async def handler(request):
        calls.append(request)
        await request.aread()
        if outcome == "lost":
            raise httpx2.ReadError("private transport detail")
        return httpx2.Response(307, headers={"Location": "https://evil.invalid/"})

    with pytest.raises(TransferClientError) as caught:
        asyncio.run(
            client(handler).upload(
                source=source, method="example", parameters={}, mime_type="application/octet-stream"
            )
        )
    assert len(calls) == 1 and "private transport detail" not in str(caught.value)


class Bytes(httpx2.AsyncByteStream):
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure = chunks, failure

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise self.failure


@pytest.mark.parametrize(
    "outcome", ["ok", "short", "long", "wrong", "interrupted", "redirect", "cancelled"]
)
def test_download_verifies_bytes_and_removes_incomplete_output(tmp_path, outcome):
    path = tmp_path / "new.apk"
    calls = []

    async def handler(request):
        calls.append(request)
        data = {"short": DATA[:-1], "long": DATA + b"x", "wrong": b"x" * len(DATA)}.get(
            outcome, DATA
        )
        failure = (
            httpx2.ReadError("private")
            if outcome == "interrupted"
            else asyncio.CancelledError()
            if outcome == "cancelled"
            else None
        )
        return httpx2.Response(
            307 if outcome == "redirect" else 200,
            stream=Bytes([data], failure),
            headers={"Location": "https://evil.invalid"},
        )

    if outcome == "ok":
        result = asyncio.run(client(handler).download(transfer_id=HANDLE, destination=path))
        assert result["state"] == "saved" and path.read_bytes() == DATA
        assert os.stat(path).st_mode & 0o777 == 0o600
    else:
        with pytest.raises((TransferClientError, asyncio.CancelledError)):
            asyncio.run(client(handler).download(transfer_id=HANDLE, destination=path))
        assert not path.exists()
    assert len(calls) == 1


def test_existing_download_destination_is_preserved(tmp_path):
    path = tmp_path / "existing"
    path.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        asyncio.run(
            client(lambda _: pytest.fail("must not send bytes")).download(
                transfer_id=HANDLE, destination=path
            )
        )
    assert path.read_bytes() == b"existing"


def test_preparation_metadata_is_reviewable_without_raw_provider_response():
    result = safe_metadata(
        {
            "operation_id": "artifact_1",
            "method": "upload",
            "effects": ["stages bytes"],
            "proposed_request": {"packageName": "com.example.app", "token": "private"},
            "file": {"size": 42, "sha256": "digest"},
            "response": {"downloadUrl": "private"},
        }
    )
    assert result["operation_id"] == "artifact_1" and result["effects"] == ["stages bytes"]
    assert result["proposed_request"]["token"] == "[redacted]"
    assert "response" not in result and "private" not in str(result)


def test_actual_sdk_result_preserves_partial_mutation_outcome_without_provider_payload():
    from pubship.hosted_transfer_client import tool

    value = {
        "mutation_succeeded": True,
        "readback_succeeded": False,
        "committed": False,
        "publication_verified": False,
        "cleanup_warning": "Cleanup remains pending",
        "observation_error": "Readback unavailable",
        "baseline_available": False,
        "readback_available": True,
        "note": "Reconcile outcome",
        "status": "unknown",
        "response": {"privateToken": "secret"},
    }
    result = safe_metadata(asyncio.run(tool(Session(value), "apply_artifact_upload", {})))
    assert result == {key: item for key, item in value.items() if key != "response"}


def test_sdk_error_explains_unknown_write_outcome():
    from pubship.hosted_transfer_client import tool

    class Rejected:
        async def call_tool(self, name, arguments):
            return CallToolResult(content=[], isError=True)

    with pytest.raises(TransferClientError, match="reconcile"):
        asyncio.run(tool(Rejected(), "apply_artifact_upload", {}))
