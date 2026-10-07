"""Independent artifact acceptance using temporary files and synthetic Google I/O."""

import asyncio
import base64
import hashlib
import io
import json
import os
import stat
import sys
import time
from copy import deepcopy
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from pubship import artifact_http
from pubship.artifact import Artifacts
from pubship.artifact_contracts import EXTERNAL, request_spec
from pubship.artifact_files import CHUNK_BYTES, FileRoots
from pubship.config import Settings
from pubship.coverage import ARTIFACT_METHODS
from pubship.errors import PlayError
from pubship.publishing import _APPLY_LOCK
from pubship.server import create_server

APP = "com.example.app"
PREFIX = "androidpublisher.edits."
PARAMS = {"packageName": APP, "editId": "owned-edit"}
APK = PREFIX + "apks.upload"
GENERATED = "androidpublisher.generatedapks.download"
SYSTEM = "androidpublisher.systemapks.variants.download"


def external_body():
    return {
        "externallyHostedApk": {
            "packageName": APP,
            "applicationLabel": "QA app",
            "versionCode": 27,
            "versionName": "27",
            "fileSize": "123",
            "fileSha1Base64": base64.b64encode(b"1" * 20).decode(),
            "fileSha256Base64": base64.b64encode(b"2" * 32).decode(),
            "iconBase64": base64.b64encode(b"icon").decode(),
            "minimumSdk": 23,
            "certificateBase64s": [base64.b64encode(b"certificate").decode()],
            "externallyHostedUrl": "https://example.test/app.apk?signature=PRIVATE",
            "usesPermissions": [],
        }
    }


@pytest.mark.parametrize(
    "identifier",
    ["provider/opaque+id==", "带 空格/:;?id=x&next=1", "a//b", "/a/"],
)
def test_opaque_download_id_is_one_encoded_segment(identifier):
    spec = request_spec(
        GENERATED, {"packageName": APP, "versionCode": 27, "downloadId": identifier}
    )
    from urllib.parse import quote

    assert spec["url"] == (
        "https://androidpublisher.googleapis.com/download/androidpublisher/v3/"
        f"applications/{APP}/generatedApks/27/downloads/{quote(identifier, safe='')}:download?alt=media"
    )
    assert parse_qs(urlsplit(spec["url"]).query) == {"alt": ["media"]}


@pytest.mark.parametrize(
    "identifier", [".", "..", "x/../y", "x/./y", "%2f", "x\\y", "x\n", "\ud800"]
)
def test_opaque_id_rejects_traversal_and_ambiguous_encoding(identifier):
    with pytest.raises(PlayError):
        request_spec(GENERATED, {"packageName": APP, "versionCode": 27, "downloadId": identifier})


@pytest.mark.parametrize("value", [True, False, 1.0, "27", 0, -1, 2**31, 10**1000])
def test_generated_version_is_strict_positive_int32(value):
    with pytest.raises(PlayError):
        request_spec(GENERATED, {"packageName": APP, "versionCode": value, "downloadId": "id"})


@pytest.mark.parametrize("value", [True, 27, "027", "0", "-1", str(2**63), "1e2"])
def test_system_version_is_strict_positive_decimal_int64_string(value):
    with pytest.raises(PlayError):
        request_spec(SYSTEM, {"packageName": APP, "versionCode": value, "variantId": 0})


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/a",
        "https://user:PRIVATE@example.test/a",
        "https://example.test:444/a",
        "https://example.test/a#PRIVATE",
        "https://example.test/\nPRIVATE",
        "https://example.test\\@other.test/a",
        "file:///PRIVATE",
        "https://example.test:invalid/a",
    ],
)
def test_external_url_restrictions_are_redacted(url):
    body = external_body()
    body["externallyHostedApk"]["externallyHostedUrl"] = url
    with pytest.raises(PlayError) as caught:
        request_spec(EXTERNAL, PARAMS, body=body)
    assert "PRIVATE" not in str(caught.value)


def test_external_nested_package_cannot_escape_outer_scope():
    body = external_body()
    body["externallyHostedApk"]["packageName"] = "com.other.app"
    with pytest.raises(PlayError):
        request_spec(EXTERNAL, PARAMS, body=body)


def test_external_declaration_preserves_explicit_empty_permissions_and_optional_arrays():
    body = external_body()
    body["externallyHostedApk"].update(nativeCodes=[], usesFeatures=[])
    original = deepcopy(body)
    spec = request_spec(EXTERNAL, PARAMS, body=body)
    assert body == original
    assert spec["http_method"] == "POST"
    assert spec["url"].endswith(f"/applications/{APP}/edits/owned-edit/apks/externallyHosted")


@pytest.fixture
def roots(tmp_path):
    upload, download = tmp_path / "upload", tmp_path / "download"
    upload.mkdir()
    download.mkdir()
    result = FileRoots(str(upload), str(download))
    yield result
    result.close()


@pytest.mark.parametrize(
    "name",
    ["../secret", "/tmp/secret", "nested/file", ".hidden", "..", "a\\b", "bad\x00", "\ud800"],
)
def test_source_paths_rejected_before_open(roots, name):
    with patch("pubship.artifact_files.os.open") as opening:
        with pytest.raises(PlayError):
            roots.snapshot(name, 1024)
    opening.assert_not_called()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "empty", "oversize"])
def test_nonregular_or_unbounded_source_rejected_without_spool(roots, tmp_path, kind):
    from pathlib import Path

    source = Path(roots.upload_root) / "artifact.apk"
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE")
    if kind == "symlink":
        source.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, source)
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "directory":
        source.mkdir()
    else:
        source.write_bytes(b"" if kind == "empty" else b"x" * 1025)
    with patch("pubship.artifact_files.tempfile.TemporaryFile") as spool:
        with pytest.raises(PlayError) as caught:
            roots.snapshot("artifact.apk", 1024)
    spool.assert_not_called()
    assert "PRIVATE" not in str(caught.value)


def test_snapshot_remains_private_and_immutable_after_source_replacement(roots):
    from pathlib import Path

    source = Path(roots.upload_root) / "artifact.apk"
    original = b"PK\x03\x04" + b"reviewed" * 100
    source.write_bytes(original)
    snapshot = roots.snapshot(source.name, 1024)
    try:
        assert stat.S_IMODE(os.fstat(snapshot.stream.fileno()).st_mode) == 0o600
        source.unlink()
        source.write_bytes(b"different")
        assert snapshot.stream.read() == original
        assert snapshot.sha256 == hashlib.sha256(original).hexdigest()
        assert snapshot.size == len(original)
    finally:
        snapshot.close()


def test_upload_root_descriptor_remains_anchored_after_path_replacement(roots, tmp_path):
    from pathlib import Path

    root = Path(roots.upload_root)
    (root / "artifact.apk").write_bytes(b"original")
    first = roots.snapshot("artifact.apk", 100)
    first.close()
    root.rename(tmp_path / "original-root")
    root.mkdir()
    (root / "artifact.apk").write_bytes(b"attacker replacement")
    second = roots.snapshot("artifact.apk", 100)
    try:
        assert second.stream.read() == b"original"
    finally:
        second.close()


def test_source_mutation_during_copy_is_detected_and_spool_closed(roots):
    import tempfile
    from pathlib import Path

    source = Path(roots.upload_root) / "artifact.apk"
    source.write_bytes(b"original")
    original_fstat, original_spool = os.fstat, tempfile.TemporaryFile
    calls, spools = 0, []

    def fstat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_bytes(b"changed and longer")
        return original_fstat(fd)

    def spool(*args, **kwargs):
        created = original_spool(*args, **kwargs)
        spools.append(created)
        return created

    with (
        patch("pubship.artifact_files.os.fstat", side_effect=fstat),
        patch("pubship.artifact_files.tempfile.TemporaryFile", side_effect=spool),
    ):
        with pytest.raises(PlayError, match="changed"):
            roots.snapshot("artifact.apk", 100)
    assert len(spools) == 1 and spools[0].closed


class MediaResponse(io.BytesIO):
    def __init__(self, data=b"PK\x03\x04synthetic APK", *, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = (
            headers if headers is not None else {"Content-Type": "application/octet-stream"}
        )
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        assert 0 < size <= CHUNK_BYTES, "artifact download must use bounded reads"
        return super().read(size)


def download_spec():
    return request_spec(
        GENERATED, {"packageName": APP, "versionCode": 27, "downloadId": "provider/id+=="}
    )


@pytest.mark.parametrize(
    "status,payload,headers",
    [
        (204, b"", {}),
        (206, b"PK\x03\x04partial", {}),
        (200, b"", {}),
        (200, b"PK\x03\x04short", {"Content-Length": "100"}),
        (200, b"PK\x03\x04long", {"Content-Length": "1"}),
        (200, b"PK\x03\x04", {"Content-Length": "-1"}),
        (200, b"PK\x03\x04", {"Content-Length": "١"}),
        (200, b"PK\x03\x04", {"Content-Length": "NaN"}),
        (200, b"PK\x03\x04", {"Content-Length": "10000"}),
        (200, b"x" * 101, {}),
        (200, b'{"error":"PRIVATE"}', {"Content-Type": "application/json"}),
        (200, b"<!doctype html><html>PRIVATE</html>", {"Content-Type": "text/html"}),
    ],
)
def test_failed_download_never_publishes_or_leaves_partial(roots, status, payload, headers):
    from pathlib import Path

    response = MediaResponse(payload, status=status, headers=headers)
    with patch("urllib.request.OpenerDirector.open", return_value=response) as send:
        with pytest.raises(PlayError) as caught:
            artifact_http.download(download_spec(), "PRIVATE-token", roots.destination(100))
    send.assert_called_once()
    assert list(Path(roots.download_root).iterdir()) == []
    assert "PRIVATE" not in str(caught.value)


def test_download_streams_to_private_generated_filename_ignoring_remote_name(roots):
    from pathlib import Path

    payload = b"PK\x03\x04" + b"x" * (CHUNK_BYTES + 31)
    response = MediaResponse(
        payload, headers={"Content-Disposition": 'attachment; filename="../../PRIVATE"'}
    )
    with patch("urllib.request.OpenerDirector.open", return_value=response) as send:
        result = artifact_http.download(
            download_spec(), "PRIVATE-token", roots.destination(len(payload))
        )
    request = send.call_args.args[0]
    assert request.full_url == download_spec()["url"]
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Authorization") == "Bearer PRIVATE-token"
    assert send.call_args.kwargs["timeout"] == 120
    target = Path(roots.download_root) / result["filename"]
    assert target.read_bytes() == payload
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert "PRIVATE" not in str(result)
    assert list(Path(roots.download_root).iterdir()) == [target]
    assert len(response.read_sizes) >= 3


@pytest.mark.parametrize("existing_kind", ["file", "symlink"])
def test_publication_collision_preserves_existing_entry_and_cleans_partial(
    roots, tmp_path, existing_kind
):
    from pathlib import Path

    destination = roots.destination(100)
    target = Path(roots.download_root) / destination.filename
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve")
    if existing_kind == "file":
        target.write_bytes(b"preserve")
    else:
        target.symlink_to(outside)
    with patch("urllib.request.OpenerDirector.open", return_value=MediaResponse()):
        with pytest.raises(PlayError):
            artifact_http.download(download_spec(), "token", destination)
    assert target.read_bytes() == outside.read_bytes() == b"preserve"
    assert target.is_symlink() == (existing_kind == "symlink")
    assert list(Path(roots.download_root).iterdir()) == [target]


@pytest.mark.parametrize("failure_point", ["fsync", "link"])
def test_disk_failure_before_publication_cleans_partial(roots, failure_point):
    from pathlib import Path

    with (
        patch("urllib.request.OpenerDirector.open", return_value=MediaResponse()),
        patch(
            "pubship.artifact_files.os." + failure_point,
            side_effect=OSError("PRIVATE disk path"),
        ),
    ):
        with pytest.raises(PlayError) as caught:
            artifact_http.download(download_spec(), "token", roots.destination(100))
    assert "PRIVATE" not in str(caught.value)
    assert list(Path(roots.download_root).iterdir()) == []


def test_download_root_descriptor_stays_anchored_after_rename(roots, tmp_path):
    from pathlib import Path

    root = Path(roots.download_root)
    destination = roots.destination(100)
    original = tmp_path / "original-download"
    root.rename(original)
    root.mkdir()
    with patch("urllib.request.OpenerDirector.open", return_value=MediaResponse()):
        result = artifact_http.download(download_spec(), "token", destination)
    assert list(root.iterdir()) == []
    assert (original / result["filename"]).read_bytes().startswith(b"PK")


def configured(roots, *, methods=ARTIFACT_METHODS, packages=frozenset({APP})):
    return Settings(
        packages=frozenset({APP}),
        artifact_packages=packages,
        artifact_methods=methods,
        artifact_upload_root=roots.upload_root,
        artifact_download_root=roots.download_root,
        artifact_max_bytes=CHUNK_BYTES * 2,
    )


@pytest.fixture
def service(roots):
    from pathlib import Path

    auth = Mock()
    auth.token.return_value = "PRIVATE-original-token"
    instance = Artifacts(configured(roots), auth, local=True)
    (Path(roots.upload_root) / "artifact.apk").write_bytes(b"PK\x03\x04reviewed bytes")
    yield instance
    instance.close()


def edit():
    return {"id": PARAMS["editId"], "expiryTimeSeconds": str(int(time.time()) + 3600)}


def prepare_apk(service, **kwargs):
    return service.prepare(
        APK,
        PARAMS,
        filename="artifact.apk",
        mime_type="application/octet-stream",
        acknowledge_effects=True,
        **kwargs,
    )


@pytest.mark.parametrize("blocked", ["package", "method", "hosted", "disabled"])
def test_unauthorized_prepare_never_probes_source_or_credentials(roots, blocked):
    settings = configured(
        roots,
        methods=frozenset({EXTERNAL}) if blocked == "method" else ARTIFACT_METHODS,
        packages=frozenset() if blocked == "disabled" else frozenset({APP}),
    )
    instance = Artifacts(settings, Mock(), local=blocked != "hosted")
    params = {**PARAMS, "packageName": "com.other.app"} if blocked == "package" else PARAMS
    try:
        with (
            patch.object(instance.files, "snapshot") as snapshot,
            patch("pubship.artifact_files.os.open") as opening,
        ):
            with pytest.raises(PlayError):
                instance.prepare(
                    APK, params, "PRIVATE.apk", "application/octet-stream", acknowledge_effects=True
                )
        snapshot.assert_not_called()
        opening.assert_not_called()
        instance.auth.token.assert_not_called()
    finally:
        instance.close()


def test_upload_uses_frozen_snapshot_original_token_exact_content_length_and_bounded_reads(service):
    from pathlib import Path

    before = {"apks": []}
    original = b"PK\x03\x04" + b"x" * (CHUNK_BYTES + 17)
    (Path(service.files.upload_root) / "artifact.apk").write_bytes(original)
    metadata = edit()
    with patch(
        "pubship.http.get",
        side_effect=[metadata, before, metadata, before, {"apks": [{"versionCode": 27}]}],
    ) as read:
        prepared = prepare_apk(service)
        snapshot = service._preparations[prepared["operation_id"]].snapshot
        (Path(service.files.upload_root) / "artifact.apk").write_bytes(b"replaced")
        service.auth.token.return_value = "PRIVATE-new-token"
        sent = []

        def upload(request, timeout):
            assert (
                request.full_url
                == f"https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/{APP}/edits/owned-edit/apks?uploadType=media"
            )
            assert request.get_method() == "POST" and timeout == 120
            assert request.get_header("Authorization") == "Bearer PRIVATE-original-token"
            assert request.get_header("Content-length") == str(len(original))
            assert request.get_header("Content-type") == "application/octet-stream"
            while chunk := request.data.read(2**30):
                assert len(chunk) <= CHUNK_BYTES
                sent.append(chunk)
            return MediaResponse(
                json.dumps(
                    {"versionCode": 27, "binary": {"sha256": hashlib.sha256(original).hexdigest()}}
                ).encode()
            )

        # Upload response is bounded JSON; its read allowance differs from the binary stream.
        with (
            patch("urllib.request.OpenerDirector.open", side_effect=upload) as send,
            patch.object(MediaResponse, "read", io.BytesIO.read),
        ):
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
        send.assert_called_once()
    assert b"".join(sent) == original
    assert snapshot.stream.closed
    assert (
        result["mutation_succeeded"] and result["readback_succeeded"] and result["digest_verified"]
    )
    assert result["artifact_observed"] and not result["committed"]
    service.auth.token.assert_called_once_with("publisher")
    assert all(call.args[1] == "PRIVATE-original-token" for call in read.call_args_list)
    assert "PRIVATE" not in str(result)


@pytest.mark.parametrize(
    "failure",
    ["disconnect", "timeout", "redirect", "http403", "http500", "malformed", "wrong_digest"],
)
def test_dispatched_upload_uncertainty_consumes_id_closes_snapshot_and_never_retries(
    service, failure
):
    metadata = edit()
    with patch(
        "pubship.http.get", side_effect=[metadata, {"apks": []}, metadata, {"apks": []}]
    ) as read:
        prepared = prepare_apk(service)
        identifier = prepared["operation_id"]
        snapshot = service._preparations[identifier].snapshot

        def dispatch(request, timeout):
            assert request.data.read(4) == b"PK\x03\x04"
            if failure == "disconnect":
                raise URLError("PRIVATE remote URL")
            if failure == "timeout":
                raise TimeoutError("PRIVATE")
            if failure == "redirect":
                raise PlayError("PRIVATE unexpected redirect")
            if failure.startswith("http"):
                raise HTTPError(
                    "https://PRIVATE", int(failure[4:]), "PRIVATE", {}, io.BytesIO(b"PRIVATE")
                )
            response = io.BytesIO(
                b"PRIVATE invalid JSON"
                if failure == "malformed"
                else json.dumps({"versionCode": 27, "binary": {"sha256": "0" * 64}}).encode()
            )
            response.status = 200
            return response

        with patch("urllib.request.OpenerDirector.open", side_effect=dispatch) as send:
            with pytest.raises(PlayError, match="outcome unknown") as caught:
                service.apply(identifier, identifier)
            with pytest.raises(PlayError, match="consumed"):
                service.apply(identifier, identifier)
        send.assert_called_once()
    assert read.call_count == 4 and snapshot.stream.closed
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("reason", ["lock", "expiry", "edit_drift", "artifact_drift"])
def test_preflight_failure_closes_consumed_snapshot_without_dispatch(service, reason):
    metadata = edit()
    with patch("pubship.http.get", side_effect=[metadata, {"apks": []}]):
        identifier = prepare_apk(service)["operation_id"]
    snapshot = service._preparations[identifier].snapshot
    if reason == "expiry":
        service._preparations[identifier].deadline = 0
    if reason == "lock":
        assert _APPLY_LOCK.acquire(blocking=False)
    try:
        with (
            patch(
                "pubship.http.get",
                side_effect=[
                    {**metadata, "extra": 1} if reason == "edit_drift" else metadata,
                    {"apks": [{"versionCode": 99}]},
                ],
            ) as read,
            patch("pubship.artifact_http.upload") as write,
        ):
            with pytest.raises(PlayError):
                service.apply(identifier, identifier)
            with pytest.raises(PlayError, match="consumed"):
                service.apply(identifier, identifier)
        write.assert_not_called()
        assert (
            read.call_count
            == {"lock": 0, "expiry": 0, "edit_drift": 1, "artifact_drift": 2}[reason]
        )
    finally:
        if reason == "lock":
            _APPLY_LOCK.release()
    assert snapshot.stream.closed


def test_failed_readback_preserves_confirmed_mutation_and_redacts_read_error(service):
    metadata = edit()
    with patch(
        "pubship.http.get",
        side_effect=[metadata, {}, metadata, {}, PlayError("PRIVATE No writes were attempted")],
    ):
        prepared = prepare_apk(service)
        assert prepared["before"] == {} and not prepared["baseline_observation_available"]
        with patch("pubship.artifact_http.upload", return_value={"versionCode": 27}) as send:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
    send.assert_called_once()
    assert result["mutation_succeeded"] is True and result["readback_succeeded"] is False
    assert result["digest_verified"] is False
    assert "after" not in result and "artifact_observed" not in result
    assert "PRIVATE" not in str(result) and "No writes were attempted" not in str(result)


def test_missing_collection_metadata_is_never_reported_as_observed_artifact(service):
    metadata = edit()
    with patch("pubship.http.get", side_effect=[metadata, {}, metadata, {}, {}]):
        prepared = prepare_apk(service)
        with patch("pubship.artifact_http.upload", return_value={"versionCode": 27}):
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert result["after"] == {} and not result["artifact_observation_available"]
    assert "artifact_observed" not in result


def test_prepare_network_failure_closes_new_private_spool(service):
    captured = []
    original = service.files.snapshot

    def capture(*args):
        snapshot = original(*args)
        captured.append(snapshot)
        return snapshot

    with (
        patch.object(service.files, "snapshot", side_effect=capture),
        patch("pubship.http.get", side_effect=PlayError("read failed")),
    ):
        with pytest.raises(PlayError):
            prepare_apk(service)
    assert len(captured) == 1 and captured[0].stream.closed
    assert not service._preparations


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_download_rejects_temporary_name_substitution_before_publication(roots, tmp_path, kind):
    from pathlib import Path

    destination = roots.destination(100)
    temporary = Path(roots.download_root) / destination.temporary
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE unrelated")

    class SwappingResponse(MediaResponse):
        def read(self, size=-1):
            chunk = super().read(size)
            if chunk:
                temporary.unlink()
                if kind == "file":
                    temporary.write_bytes(b"PRIVATE replacement")
                else:
                    temporary.symlink_to(outside)
            return chunk

    with patch("urllib.request.OpenerDirector.open", return_value=SwappingResponse()):
        with pytest.raises(PlayError):
            artifact_http.download(download_spec(), "token", destination)
    assert not (Path(roots.download_root) / destination.filename).exists()
    assert outside.read_bytes() == b"PRIVATE unrelated"


def test_post_publication_cleanup_failure_cannot_claim_no_completed_file(roots):
    from pathlib import Path

    destination = roots.destination(100)
    original_unlink = os.unlink

    def unlink(name, **kwargs):
        if name == destination.temporary:
            raise OSError("PRIVATE cleanup failure")
        return original_unlink(name, **kwargs)

    with (
        patch("urllib.request.OpenerDirector.open", return_value=MediaResponse()),
        patch("pubship.artifact_files.os.unlink", side_effect=unlink),
    ):
        try:
            result = artifact_http.download(download_spec(), "token", destination)
        except PlayError as error:
            assert not (Path(roots.download_root) / destination.filename).exists(), (
                "An error claiming no completed file must not leave a published output"
            )
            assert "PRIVATE" not in str(error)
        else:
            assert result["filename"] == destination.filename
            assert (Path(roots.download_root) / result["filename"]).read_bytes().startswith(b"PK")
            assert "PRIVATE" not in str(result)


@pytest.mark.parametrize("failure", ["disconnect", "timeout", "deadline", "disk_full"])
def test_download_midstream_failure_closes_response_and_removes_partial(roots, failure):
    from pathlib import Path

    clock = [0]

    class InterruptedResponse(MediaResponse):
        def read(self, size=-1):
            if self.tell():
                if failure == "disconnect":
                    raise URLError("PRIVATE remote URL")
                if failure == "timeout":
                    raise TimeoutError("PRIVATE")
            data = super().read(size)
            if failure == "deadline":
                clock[0] = 3601
            return data

    response = InterruptedResponse()
    destination = roots.destination(100)
    original_write = destination.write

    def write(chunk):
        original_write(chunk)
        if failure == "disk_full":
            raise OSError("PRIVATE disk full")

    with (
        patch("urllib.request.OpenerDirector.open", return_value=response) as send,
        patch("pubship.artifact_http.time.monotonic", side_effect=lambda: clock[0]),
        patch.object(destination, "write", side_effect=write),
    ):
        with pytest.raises(PlayError) as caught:
            artifact_http.download(download_spec(), "token", destination)
    send.assert_called_once()
    assert response.closed and destination.stream.closed
    assert not list(Path(roots.download_root).iterdir())
    assert "PRIVATE" not in str(caught.value)


def test_default_and_hosted_servers_never_expose_artifact_filesystem_tools(roots):
    names = {"prepare_artifact_upload", "apply_artifact_upload", "download_artifact"}
    factory = Mock(side_effect=AssertionError("hosted discovery must not resolve credentials"))

    async def exercise():
        for server in (create_server(Settings()), create_server(service_factory=factory)):
            async with Client(server) as client:
                assert not names.intersection(
                    tool.name for tool in (await client.list_tools()).tools
                )

    with patch("pubship.artifact_files.os.open") as opening:
        asyncio.run(exercise())
    factory.assert_not_called()
    opening.assert_not_called()


def test_real_stdio_upload_download_review_and_replay_with_synthetic_transport(tmp_path, roots):
    from pathlib import Path

    payload = b"PK\x03\x04synthetic APK"
    (Path(roots.upload_root) / "artifact.apk").write_bytes(payload)
    launcher = tmp_path / "synthetic_server.py"
    launcher.write_text("""
import hashlib, io, json, time, urllib.request
from pubship.auth import Auth
from pubship.server import main
Auth.token = lambda self, audience: "synthetic-token"
payload = b"PK\\x03\\x04synthetic APK"
edit = {"id": "owned-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
uploaded = False
class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/octet-stream"}
def send(self, request, timeout):
    global uploaded
    assert request.get_header("Authorization") == "Bearer synthetic-token"
    if "/upload/" in request.full_url:
        assert request.get_method() == "POST"
        chunks = []
        while chunk := request.data.read(1024):
            chunks.append(chunk)
        assert b"".join(chunks) == payload
        uploaded = True
        value = {"versionCode": 27, "binary": {"sha256": hashlib.sha256(payload).hexdigest()}}
    elif "/download/" in request.full_url:
        assert request.get_method() == "GET" and request.full_url.endswith("?alt=media")
        return Response(payload)
    elif request.full_url.endswith("/apks"):
        value = {"apks": [{"versionCode": 27}] if uploaded else []}
    else:
        assert request.full_url.endswith("/edits/owned-edit")
        value = edit
    return Response(json.dumps(value).encode())
urllib.request.OpenerDirector.open = send
main()
""")

    async def exercise():
        connection = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher)],
            env={
                "GOOGLE_PLAY_PACKAGES": APP,
                "GOOGLE_PLAY_ARTIFACT_PACKAGES": APP,
                "GOOGLE_PLAY_ARTIFACT_METHODS": ",".join([APK, GENERATED]),
                "GOOGLE_PLAY_ARTIFACT_UPLOAD_ROOT": roots.upload_root,
                "GOOGLE_PLAY_ARTIFACT_DOWNLOAD_ROOT": roots.download_root,
                "GOOGLE_PLAY_AUTH_MODE": "adc",
                "GOOGLE_PLAY_WRITE_PACKAGES": "",
                "GOOGLE_PLAY_EDIT_PACKAGES": "",
                "GOOGLE_PLAY_TRACK_PACKAGES": "",
                "GOOGLE_PLAY_EDIT_WRITE_PACKAGES": "",
            },
        )
        async with Client(connection) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("prepare_artifact_upload", "apply_artifact_upload", "download_artifact"):
                assert not tools[name].annotations.read_only_hint
                assert not tools[name].annotations.idempotent_hint
            assert not tools["prepare_artifact_upload"].annotations.destructive_hint
            assert not tools["download_artifact"].annotations.destructive_hint
            assert tools["apply_artifact_upload"].annotations.destructive_hint
            args = {
                "method": APK,
                "parameters": PARAMS,
                "filename": "artifact.apk",
                "mime_type": "application/octet-stream",
                "acknowledge_effects": True,
            }
            invalid = await client.call_tool(
                "prepare_artifact_upload",
                {**args, "acknowledge_effects": 1, "unreviewed": "PRIVATE input"},
            )
            assert invalid.is_error and "PRIVATE" not in str(invalid.content)
            prepared = await client.call_tool("prepare_artifact_upload", args)
            assert not prepared.is_error, prepared.content
            identifier = prepared.structured_content["operation_id"]
            applied = await client.call_tool(
                "apply_artifact_upload", {"operation_id": identifier, "confirmation": identifier}
            )
            assert not applied.is_error, applied.content
            assert applied.structured_content["mutation_succeeded"]
            assert applied.structured_content["artifact_observed"]
            assert applied.structured_content["digest_verified"]
            replay = await client.call_tool(
                "apply_artifact_upload", {"operation_id": identifier, "confirmation": identifier}
            )
            assert replay.is_error
            downloaded = await client.call_tool(
                "download_artifact",
                {
                    "method": GENERATED,
                    "parameters": {
                        "packageName": APP,
                        "versionCode": 27,
                        "downloadId": "opaque/id+==",
                    },
                },
            )
            assert not downloaded.is_error, downloaded.content
            result = downloaded.structured_content
            assert not result["google_mutation_performed"]
            assert (Path(roots.download_root) / result["filename"]).read_bytes() == payload

    asyncio.run(exercise())
