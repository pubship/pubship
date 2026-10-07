"""Synthetic artifact contract, streaming and lifecycle regression evidence."""

import base64
import hashlib
import io
import json
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from pubship import artifact_http
from pubship.artifact import Artifacts
from pubship.artifact_contracts import (
    CAPS,
    EXTERNAL,
    PREFIX,
    baseline_specs,
    external_body,
    request_spec,
    validate_baseline,
    validate_response,
)
from pubship.artifact_files import Snapshot
from pubship.config import Settings
from pubship.coverage import ARTIFACT_METHODS
from pubship.errors import PlayError

PACKAGE = "com.example.app"
PARAMS = {"packageName": PACKAGE, "editId": "existing-edit"}
PAYLOAD = b"PK\x03\x04synthetic-artifact"


def external():
    return {
        "externallyHostedApk": {
            "packageName": PACKAGE,
            "applicationLabel": "Example",
            "versionCode": 7,
            "versionName": "1.0",
            "fileSize": "24",
            "fileSha1Base64": base64.b64encode(b"a" * 20).decode(),
            "fileSha256Base64": base64.b64encode(b"b" * 32).decode(),
            "iconBase64": "aWNvbg==",
            "minimumSdk": 21,
            "certificateBase64s": ["Y2VydA=="],
            "externallyHostedUrl": "https://artifacts.example/app.apk",
            "usesPermissions": [],
        }
    }


def snapshot():
    return Snapshot(
        io.BytesIO(PAYLOAD),
        len(PAYLOAD),
        hashlib.sha1(PAYLOAD).hexdigest(),
        hashlib.sha256(PAYLOAD).hexdigest(),
    )


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "source.bin").write_bytes(PAYLOAD)
    settings = Settings(
        packages=frozenset({PACKAGE}),
        artifact_packages=frozenset({PACKAGE}),
        artifact_methods=ARTIFACT_METHODS,
        artifact_upload_root=str(tmp_path),
        artifact_download_root=str(tmp_path),
        artifact_max_bytes=1024,
    )
    auth = Mock()
    auth.token.return_value = "synthetic-token"
    service = Artifacts(settings, auth, local=True)
    yield service, auth, tmp_path
    service.close()


@pytest.mark.parametrize(
    "suffix,extra,mime,ending",
    [
        ("apks.upload", {}, "application/vnd.android.package-archive", "/apks?uploadType=media"),
        (
            "bundles.upload",
            {"deviceTierConfigId": "LATEST", "ackBundleInstallationWarning": False},
            "application/octet-stream",
            "/bundles?deviceTierConfigId=LATEST&ackBundleInstallationWarning=false&uploadType=media",
        ),
        (
            "deobfuscationfiles.upload",
            {"apkVersionCode": 7, "deobfuscationFileType": "nativeCode"},
            "application/octet-stream",
            "/apks/7/deobfuscationFiles/nativeCode?uploadType=media",
        ),
        (
            "expansionfiles.upload",
            {"apkVersionCode": 7, "expansionFileType": "main"},
            "application/octet-stream",
            "/apks/7/expansionFiles/main?uploadType=media",
        ),
        (
            "images.upload",
            {
                "language": "en-US",
                "imageType": "icon",
                "aiGeneratedState": "aiGeneratedStateNotAiGenerated",
            },
            "image/png",
            "/listings/en-US/icon?aiGeneratedState=aiGeneratedStateNotAiGenerated&uploadType=media",
        ),
    ],
)
def test_binary_routes_and_streamed_exact_bytes(monkeypatch, suffix, extra, mime, ending):
    spec = request_spec(PREFIX + suffix, {**PARAMS, **extra}, filename="source.bin", mime_type=mime)
    assert spec["url"].startswith(
        "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/"
    )
    assert spec["url"].endswith(ending)
    response = Mock(status=200)
    response.read.return_value = b'{"versionCode":7}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    seen = {}

    def open_request(req, timeout):
        seen.update(url=req.full_url, headers=dict(req.header_items()), timeout=timeout)
        chunks = []
        while chunk := req.data.read(5):
            chunks.append(chunk)
        assert b"".join(chunks) == PAYLOAD
        assert all(len(c) <= 5 for c in chunks)
        return response

    monkeypatch.setattr(
        artifact_http.urllib.request,
        "build_opener",
        lambda *_: Mock(handlers=[], open=open_request),
    )
    assert artifact_http.upload(spec, "synthetic-token", snapshot()) == {"versionCode": 7}
    assert seen["headers"]["Content-length"] == str(len(PAYLOAD))
    assert seen["headers"]["Content-type"] == mime
    assert seen["timeout"] == 120


@pytest.mark.parametrize(
    "method,extra",
    [
        (PREFIX + "apks.upload", {}),
        (PREFIX + "bundles.upload", {}),
        (
            PREFIX + "deobfuscationfiles.upload",
            {"apkVersionCode": 7, "deobfuscationFileType": "proguard"},
        ),
        (PREFIX + "expansionfiles.upload", {"apkVersionCode": 7, "expansionFileType": "patch"}),
        (PREFIX + "images.upload", {"language": "en-US", "imageType": "phoneScreenshots"}),
    ],
)
def test_full_upload_lifecycle(setup, monkeypatch, method, extra):
    service, auth, _ = setup
    edit = {"id": "existing-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    if method.endswith("bundles.upload"):
        before, after, mutation = (
            {"bundles": []},
            {"bundles": [{"versionCode": 7}]},
            {"versionCode": 7, "sha256": snapshot().sha256},
        )
    elif method.endswith("images.upload"):
        before, after, mutation = (
            {"images": []},
            {"images": [{"id": "image-7"}]},
            {"image": {"id": "image-7"}},
        )
    elif method.endswith("expansionfiles.upload"):
        before, after, mutation = (
            {},
            {"fileSize": str(len(PAYLOAD))},
            {"expansionFile": {"fileSize": str(len(PAYLOAD))}},
        )
    else:
        before, after = {"apks": [{"versionCode": 7}]}, {"apks": [{"versionCode": 7}]}
        mutation = (
            {"deobfuscationFile": {"symbolType": "proguard"}}
            if "deobfuscation" in method
            else {"versionCode": 7, "binary": {"sha1": snapshot().sha1}}
        )
    reads = [edit, before]
    if method.endswith("images.upload"):
        reads += [{"listings": [{"language": "en-US"}]}]
    if method.endswith("deobfuscationfiles.upload"):
        reads += [{"bundles": []}]
    reads += [edit, before]
    if method.endswith("images.upload"):
        reads += [{"listings": [{"language": "en-US"}]}]
    if method.endswith("deobfuscationfiles.upload"):
        reads += [{"bundles": []}]
    reads += [after]
    if method.endswith("deobfuscationfiles.upload"):
        reads += [{"bundles": []}]
    if method.endswith("images.upload"):
        reads += [{"listings": [{"language": "en-US"}]}]
    get = Mock(side_effect=reads)
    upload = Mock(return_value=mutation)
    monkeypatch.setattr("pubship.artifact.http.get", get)
    monkeypatch.setattr(artifact_http, "upload", upload)
    prepared = service.prepare(
        method,
        {**PARAMS, **extra},
        "source.bin",
        "image/png" if "images" in method else "application/octet-stream",
        acknowledge_effects=True,
    )
    state = service._preparations[prepared["operation_id"]]
    assert prepared["file"] == snapshot().metadata()
    result = service.apply(prepared["operation_id"], prepared["operation_id"])
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert result["after"] == after
    assert state.snapshot.stream.closed
    assert auth.token.call_count == 1
    assert upload.call_count == 1
    assert all(call.args[1] == "synthetic-token" for call in get.call_args_list)
    with pytest.raises(PlayError, match="consumed"):
        service.apply(prepared["operation_id"], prepared["operation_id"])


@pytest.mark.parametrize(
    "mime",
    [
        "image/*",
        "image/png; charset=utf-8",
        "image/png\r\nBad: true",
        "application/octet-stream",
        "image/",
        "image/é",
    ],
)
def test_bad_image_mime(mime):
    with pytest.raises(PlayError):
        request_spec(
            PREFIX + "images.upload",
            {**PARAMS, "language": "en-US", "imageType": "icon"},
            filename="source.bin",
            mime_type=mime,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("apkVersionCode", True),
        ("apkVersionCode", 1.0),
        ("apkVersionCode", 0),
        ("apkVersionCode", 2**31),
        ("deobfuscationFileType", "deobfuscationFileTypeUnspecified"),
    ],
)
def test_strict_version_and_types(field, value):
    parameters = {**PARAMS, "apkVersionCode": 7, "deobfuscationFileType": "proguard", field: value}
    with pytest.raises(PlayError):
        request_spec(
            PREFIX + "deobfuscationfiles.upload",
            parameters,
            filename="source.bin",
            mime_type="application/octet-stream",
        )


@pytest.mark.parametrize("value", [True, 1, "0", "-1", "01", str(2**63), "1.0"])
def test_bundle_config_int64(value):
    with pytest.raises(PlayError):
        request_spec(
            PREFIX + "bundles.upload",
            {**PARAMS, "deviceTierConfigId": value},
            filename="source.bin",
            mime_type="application/octet-stream",
        )


def test_external_large_icon_and_exact_body_transport(monkeypatch):
    body = external()
    body["externallyHostedApk"]["iconBase64"] = base64.b64encode(b"x" * 100000).decode()
    spec = request_spec(EXTERNAL, PARAMS, body=body)
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(body).encode()

    def execute(req, timeout):
        assert req.full_url.endswith("/apks/externallyHosted")
        assert json.loads(req.data) == body
        assert req.get_header("Content-length") == str(len(req.data))
        return response

    monkeypatch.setattr(
        artifact_http.urllib.request, "build_opener", lambda *_: Mock(handlers=[], open=execute)
    )
    assert artifact_http.upload(spec, "synthetic-token", body=body) == body


@pytest.mark.parametrize(
    "change",
    [
        {"packageName": "com.other.app"},
        {"fileSha1Base64": "bad"},
        {"minimumSdk": True},
        {"fileSha256Base64": "YQ=="},
        {"certificateBase64s": []},
        {"usesPermissions": None},
        {"fileSize": "0"},
        {"maximumSdk": 1},
        {"usesPermissions": [{"name": "x", "maxSdkVersion": False}]},
        {"unknown": "field"},
    ],
)
def test_external_invalid_declarations(change):
    body = external()
    body["externallyHostedApk"].update(change)
    with pytest.raises(PlayError):
        external_body(body, PACKAGE)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "https://user:pass@example.com/a",
        "https://example.com:444/a",
        "https://example.com/a#fragment",
        "https://example.com/a\n",
        "https://example.com\\bad/a",
        "https:///a",
    ],
)
def test_external_url_restrictions(url):
    body = external()
    body["externallyHostedApk"]["externallyHostedUrl"] = url
    with pytest.raises(PlayError, match="URL"):
        external_body(body, PACKAGE)


@pytest.mark.parametrize(
    "data",
    [
        {"apks": None},
        {"apks": [{}]},
        {"apks": [{"versionCode": True}]},
        {"apks": [{"versionCode": 1}, {"versionCode": 1}]},
    ],
)
def test_baseline_malformed_not_absent(data):
    with pytest.raises(PlayError):
        validate_baseline(baseline_specs(PREFIX + "apks.upload", PARAMS)[0], data)


def test_baseline_omission_retained_as_unavailable():
    spec = baseline_specs(PREFIX + "apks.upload", PARAMS)[0]
    assert validate_baseline(spec, {}) is False
    assert validate_baseline(spec, {"apks": []}) is True


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"versionCode": True},
        {"versionCode": 1, "binary": None},
        {"versionCode": 1, "binary": {"sha1": "0" * 40}},
        {"versionCode": 1, "binary": {"sha256": "bad"}},
    ],
)
def test_upload_response_identity_and_checksum(data):
    with pytest.raises(PlayError):
        validate_response(PREFIX + "apks.upload", PARAMS, None, snapshot(), data)


def test_absent_digest_does_not_claim_verification():
    assert (
        validate_response(PREFIX + "apks.upload", PARAMS, None, snapshot(), {"versionCode": 1})
        is False
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_packages", frozenset({"com.other.app"})),
        ("artifact_methods", frozenset({"*"})),
        ("artifact_max_bytes", True),
        ("artifact_max_bytes", 0),
        ("artifact_max_bytes", 53687091201),
        ("artifact_upload_root", "relative"),
    ],
)
def test_configuration_fail_closed(setup, field, value):
    with pytest.raises(PlayError):
        replace(setup[0].settings, **{field: value})


def test_expired_preparation_purge_closes_snapshot(setup, monkeypatch):
    service, _, _ = setup
    edit = {"id": "existing-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    monkeypatch.setattr("pubship.artifact.http.get", Mock(side_effect=[edit, {}, edit, {}]))
    first = service.prepare(
        PREFIX + "apks.upload",
        PARAMS,
        "source.bin",
        "application/octet-stream",
        acknowledge_effects=True,
    )
    retained = service._preparations[first["operation_id"]]
    retained.deadline = 0
    service.prepare(
        PREFIX + "apks.upload",
        PARAMS,
        "source.bin",
        "application/octet-stream",
        acknowledge_effects=True,
    )
    assert retained.snapshot.stream.closed


def test_caps_are_provider_values():
    assert CAPS[PREFIX + "apks.upload"] == 10737418240
    assert CAPS[PREFIX + "bundles.upload"] == 53687091200
    assert CAPS[PREFIX + "deobfuscationfiles.upload"] == 2097152000
    assert CAPS[PREFIX + "expansionfiles.upload"] == 2147483648
    assert CAPS[PREFIX + "images.upload"] == 15728640


def test_aggregate_reservation_includes_active_upload(setup, monkeypatch):
    service, _, root = setup
    service.settings = replace(service.settings, artifact_max_bytes=len(PAYLOAD))
    edit = {"id": "existing-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    monkeypatch.setattr(
        "pubship.artifact.http.get",
        lambda url, _: edit if url.endswith("/existing-edit") else {},
    )
    args = (PREFIX + "apks.upload", PARAMS, "source.bin", "application/octet-stream")
    first = service.prepare(*args, acknowledge_effects=True)

    def in_flight(*_):
        service.prepare(*args, acknowledge_effects=True)
        with pytest.raises(PlayError, match="byte limit"):
            service.prepare(*args, acknowledge_effects=True)
        assert service._retained_bytes == 2 * len(PAYLOAD)
        return {"versionCode": 7}

    monkeypatch.setattr(artifact_http, "upload", in_flight)
    result = service.apply(first["operation_id"], first["operation_id"])
    assert result["mutation_succeeded"]
    assert service._retained_bytes == len(PAYLOAD)


def test_snapshot_cleanup_failure_preserves_result_and_releases_lock(setup, monkeypatch):
    from pubship.publishing import _APPLY_LOCK

    service, _, _ = setup
    edit = {"id": "existing-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    monkeypatch.setattr("pubship.artifact.http.get", Mock(side_effect=[edit, {}, edit, {}, {}]))
    monkeypatch.setattr(artifact_http, "upload", Mock(return_value={"versionCode": 7}))
    prepared = service.prepare(
        PREFIX + "apks.upload",
        PARAMS,
        "source.bin",
        "application/octet-stream",
        acknowledge_effects=True,
    )
    preparation = service._preparations[prepared["operation_id"]]
    original_close = preparation.close

    def failed_cleanup():
        original_close()
        raise OSError("private path must not leak")

    monkeypatch.setattr(preparation, "close", failed_cleanup)
    result = service.apply(prepared["operation_id"], prepared["operation_id"])
    assert result["mutation_succeeded"] and "cleanup_warning" in result
    assert "private path" not in str(result)
    assert _APPLY_LOCK.acquire(blocking=False)
    _APPLY_LOCK.release()


def test_failed_close_quarantines_snapshot_until_local_cleanup(setup, monkeypatch):
    service, _, _ = setup
    edit = {"id": "existing-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    monkeypatch.setattr("pubship.artifact.http.get", Mock(side_effect=[edit, {}, edit, {}, {}]))
    upload = Mock(return_value={"versionCode": 7})
    monkeypatch.setattr(artifact_http, "upload", upload)
    prepared = service.prepare(
        PREFIX + "apks.upload",
        PARAMS,
        "source.bin",
        "application/octet-stream",
        acknowledge_effects=True,
    )
    preparation = service._preparations[prepared["operation_id"]]
    original_close = preparation.close
    monkeypatch.setattr(preparation, "close", Mock(side_effect=OSError("private path")))
    result = service.apply(prepared["operation_id"], prepared["operation_id"])
    assert result["mutation_succeeded"] and result["cleanup_warning"]
    assert not preparation.snapshot.stream.closed
    assert service._retained_bytes == len(PAYLOAD)
    assert service._retained_count == 1
    assert service._cleanup_pending == [preparation]
    monkeypatch.setattr(preparation, "close", original_close)
    with service._lock:
        service._purge()
    assert preparation.snapshot.stream.closed
    assert service._retained_bytes == service._retained_count == 0
    assert not service._cleanup_pending
    assert upload.call_count == 1
