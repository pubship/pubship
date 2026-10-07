"""Synthetic internal sharing uploads: scoped files, one-shot effects and wire transport."""

import hashlib
import io
import json
import os
import time
import urllib.error
from dataclasses import replace
from unittest.mock import Mock

import pytest

from pubship import artifact_http
from pubship.config import Settings
from pubship.contracts import methods
from pubship.errors import PlayError
from pubship.http import NoRedirect
from pubship.publishing import _APPLY_LOCK
from pubship.sharing import CAPS, PREFIX, InternalSharing

PACKAGE = "com.example.app"
APK = PREFIX + "uploadapk"
BUNDLE = PREFIX + "uploadbundle"
PAYLOAD = b"synthetic-sharing-file"
RESPONSE = {
    "downloadUrl": "https://play.google.com/apps/test/synthetic",
    "sha256": "b" * 64,
    "certificateFingerprint": "c" * 64,
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "source.bin").write_bytes(PAYLOAD)
    settings = Settings(
        packages=frozenset({PACKAGE}),
        artifact_packages=frozenset({PACKAGE}),
        artifact_methods=frozenset(CAPS),
        artifact_upload_root=str(tmp_path),
        artifact_max_bytes=1024,
    )
    auth = Mock()
    auth.token.return_value = "synthetic-original-token"
    service = InternalSharing(settings, auth, local=True)
    # Any unrelated API read or actual network call is a test failure.
    monkeypatch.setattr("pubship.http.get", Mock(side_effect=AssertionError("No reads")))
    monkeypatch.setattr(
        artifact_http.urllib.request,
        "build_opener",
        Mock(side_effect=AssertionError("No real uploads")),
    )
    yield service, auth, tmp_path
    service.close()


def prepare(service, method=APK):
    return service.prepare(method, PACKAGE, "source.bin", acknowledge_sharing_effects=True)


@pytest.mark.parametrize("method,ending", [(APK, "apk"), (BUNDLE, "bundle")])
def test_direct_upload_frozen_bytes_and_original_credential(setup, monkeypatch, method, ending):
    service, auth, root = setup
    prepared = prepare(service, method)
    operation_id = prepared["operation_id"]
    retained = service._preparations[operation_id]
    assert 0 < retained.deadline - time.monotonic() <= 600
    assert not prepared["executed"]
    assert prepared["source_file"]["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    (root / "source.bin").write_bytes(b"replaced-after-prepare")
    auth.token.return_value = "synthetic-new-token"

    def upload(spec, token, snapshot):
        assert spec["url"] == (
            "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/"
            f"internalappsharing/{PACKAGE}/artifacts/{ending}?uploadType=media"
        )
        assert spec["http_method"] == "POST"
        assert spec["mime_type"] == "application/octet-stream"
        assert token == "synthetic-original-token"
        assert snapshot.stream.read() == PAYLOAD
        return dict(RESPONSE)

    upload_mock = Mock(side_effect=upload)
    monkeypatch.setattr(artifact_http, "upload", upload_mock)
    result = service.apply(operation_id, operation_id)
    assert result["mutation_succeeded"]
    assert result["mutation_response"] == RESPONSE
    assert result["source_file"]["sha256"] != RESPONSE["sha256"]
    assert "digest_verified" not in result
    assert result["publication_verified"] is False
    assert result["data_scope"] == "internal_app_sharing"
    assert retained.snapshot.stream.closed
    assert service._retained_bytes == service._retained_count == 0
    auth.token.assert_called_once_with("publisher")
    upload_mock.assert_called_once()
    with pytest.raises(PlayError, match="consumed"):
        service.apply(operation_id, operation_id)


@pytest.mark.parametrize("ack", [False, 1, "true", None])
def test_acknowledgement_must_be_literal_true(setup, ack):
    service, auth, _ = setup
    with pytest.raises(PlayError, match="acknowledge_sharing_effects"):
        service.prepare(APK, PACKAGE, "source.bin", ack)
    auth.token.assert_not_called()


@pytest.mark.parametrize("local", [False, None, 1, "true"])
def test_local_only(setup, local):
    service, auth, _ = setup
    service._local = local
    with pytest.raises(PlayError, match="explicit local"):
        prepare(service)
    auth.token.assert_not_called()


@pytest.mark.parametrize(
    "method,package,filename",
    [
        ("androidpublisher.edits.apks.upload", PACKAGE, "source.bin"),
        (APK, "com.other.app", "source.bin"),
        (APK, "com.example.app/../../evil", "source.bin"),
        (APK, PACKAGE, "../source.bin"),
        (APK, PACKAGE, "/source.bin"),
        (APK, PACKAGE, ".secret"),
        (APK, PACKAGE, "a\\source.bin"),
        (APK, PACKAGE, "missing.bin"),
    ],
)
def test_rejects_unscoped_requests_before_auth(setup, method, package, filename):
    service, auth, _ = setup
    with pytest.raises(PlayError):
        service.prepare(method, package, filename, True)
    auth.token.assert_not_called()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "empty", "oversize"])
def test_non_regular_and_unbounded_sources_fail_closed(setup, kind):
    service, auth, root = setup
    source = root / "bad.bin"
    if kind == "symlink":
        source.symlink_to(root / "source.bin")
    elif kind == "hardlink":
        os.link(root / "source.bin", source)
    elif kind == "directory":
        source.mkdir()
    else:
        source.write_bytes(b"" if kind == "empty" else b"x" * 1025)
    with pytest.raises(PlayError):
        service.prepare(APK, PACKAGE, "bad.bin", True)
    auth.token.assert_not_called()


def test_wrong_confirmation_does_not_consume(setup, monkeypatch):
    service, _, _ = setup
    operation = prepare(service)["operation_id"]
    with pytest.raises(PlayError, match="Confirmation"):
        service.apply(operation, "yes")
    assert operation in service._preparations
    monkeypatch.setattr(artifact_http, "upload", Mock(return_value=dict(RESPONSE)))
    assert service.apply(operation, operation)["executed"]


@pytest.mark.parametrize("failure", ["expiry", "revoked", "lock"])
def test_prewrite_failure_consumes_and_cleans_without_upload(setup, monkeypatch, failure):
    service, _, _ = setup
    operation = prepare(service)["operation_id"]
    retained = service._preparations[operation]
    upload = Mock()
    monkeypatch.setattr(artifact_http, "upload", upload)
    if failure == "expiry":
        retained.deadline = 0
    elif failure == "revoked":
        service.settings = replace(service.settings, artifact_methods=frozenset({BUNDLE}))
    else:
        assert _APPLY_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(PlayError):
            service.apply(operation, operation)
    finally:
        if failure == "lock":
            _APPLY_LOCK.release()
    assert retained.snapshot.stream.closed
    assert operation not in service._preparations
    upload.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"downloadUrl": "http://unsafe.test"},
        {"downloadUrl": 2},
        {"downloadUrl": "https://user:password@example.test"},
        {"downloadUrl": "https://example.test:invalid"},
        {"downloadUrl": "https://example.test/\nprivate"},
        {**RESPONSE, "sha256": "invalid"},
        {**RESPONSE, "certificateFingerprint": []},
    ],
)
def test_malformed_response_is_uncertain_and_never_retried(setup, monkeypatch, response):
    service, _, _ = setup
    operation = prepare(service)["operation_id"]
    upload = Mock(return_value=response)
    monkeypatch.setattr(artifact_http, "upload", upload)
    with pytest.raises(PlayError, match="outcome unknown") as caught:
        service.apply(operation, operation)
    assert "Do not automatically retry" in str(caught.value)
    assert "edit" not in str(caught.value)
    upload.assert_called_once()
    with pytest.raises(PlayError, match="consumed"):
        service.apply(operation, operation)


def test_expiry_purge_and_retained_capacity(setup):
    service, _, _ = setup
    service.settings = replace(service.settings, artifact_max_bytes=len(PAYLOAD))
    first = prepare(service)["operation_id"]
    retained = service._preparations[first]
    prepare(service)
    with pytest.raises(PlayError, match="byte limit"):
        prepare(service)
    retained.deadline = 0
    prepare(service)
    assert retained.snapshot.stream.closed
    assert service._retained_bytes == 2 * len(PAYLOAD)


def test_preparation_count_limit(setup):
    service, _, _ = setup
    for _ in range(4):
        prepare(service)
    with pytest.raises(PlayError, match="capacity"):
        prepare(service)


def test_operation_id_collision_preserves_both_private_snapshots(setup, monkeypatch):
    service, _, root = setup
    monkeypatch.setattr(
        "pubship.sharing.secrets.token_urlsafe",
        Mock(side_effect=["a" * 43, "a" * 43, "b" * 43]),
    )
    first = prepare(service)["operation_id"]
    first_snapshot = service._preparations[first].snapshot
    changed = b"different synthetic payload"
    (root / "source.bin").write_bytes(changed)
    second = prepare(service)["operation_id"]
    assert first != second
    assert first_snapshot.stream.read() == PAYLOAD
    assert service._preparations[second].snapshot.stream.read() == changed
    assert service._retained_bytes == len(PAYLOAD) + len(changed)
    assert service._retained_count == 2
    service.close()
    assert first_snapshot.stream.closed
    assert service._retained_bytes == service._retained_count == 0


@pytest.mark.parametrize("known", [False, True])
def test_apply_purges_other_expired_snapshots_even_for_unknown_id(setup, monkeypatch, known):
    service, _, _ = setup
    expired = prepare(service)["operation_id"]
    retained = service._preparations[expired]
    operation = prepare(service)["operation_id"] if known else "sharing_" + "z" * 43
    retained.deadline = 0
    monkeypatch.setattr(artifact_http, "upload", Mock(return_value=dict(RESPONSE)))
    if known:
        assert service.apply(operation, operation)["executed"]
    else:
        with pytest.raises(PlayError, match="Unknown"):
            service.apply(operation, operation)
    assert retained.snapshot.stream.closed
    assert not service._preparations
    assert service._retained_bytes == service._retained_count == 0


def test_failed_cleanup_keeps_reservation_then_recovers(setup, monkeypatch):
    service, _, _ = setup
    operation = prepare(service)["operation_id"]
    retained = service._preparations[operation]
    close = retained.close
    monkeypatch.setattr(retained, "close", Mock(side_effect=OSError("private path")))
    monkeypatch.setattr(artifact_http, "upload", Mock(return_value=dict(RESPONSE)))
    result = service.apply(operation, operation)
    assert result["mutation_succeeded"] and result["cleanup_warning"]
    assert "private path" not in str(result)
    assert service._retained_bytes == len(PAYLOAD)
    assert service._cleanup_pending == [retained]
    assert _APPLY_LOCK.acquire(blocking=False)
    _APPLY_LOCK.release()
    monkeypatch.setattr(retained, "close", close)
    service.close()
    assert retained.snapshot.stream.closed
    assert service._retained_bytes == service._retained_count == 0


def test_provider_caps_match_pinned_discovery():
    for method, cap in CAPS.items():
        assert cap == int(methods()[method]["mediaUpload"]["maxSize"])


def test_single_media_request_and_no_download_of_response_url(setup, monkeypatch):
    service, _, _ = setup
    operation = prepare(service)["operation_id"]
    response = Mock()
    response.status = 200
    response.read.return_value = json.dumps(RESPONSE).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock(handlers=[])

    def open_request(req, timeout):
        assert req.method == "POST"
        assert req.get_header("Content-type") == "application/octet-stream"
        assert req.get_header("Content-length") == str(len(PAYLOAD))
        assert req.get_header("Authorization") == "Bearer synthetic-original-token"
        assert timeout == 120
        assert req.data.read() == PAYLOAD
        assert req.data.read() == b""
        return response

    opener.open.side_effect = open_request

    def build(handler, *bounded_handlers):
        assert isinstance(handler, NoRedirect) and len(bounded_handlers) == 2
        return opener

    monkeypatch.setattr(artifact_http.urllib.request, "build_opener", build)
    assert service.apply(operation, operation)["mutation_response"] == RESPONSE
    opener.open.assert_called_once()


@pytest.mark.parametrize("failure", ["timeout", "http", "redirect", "invalid_json", "oversize"])
def test_transport_failure_is_uncertain_and_single_attempt(setup, monkeypatch, failure):
    service, _, _ = setup
    operation = prepare(service)["operation_id"]
    opener = Mock(handlers=[])
    response = Mock()
    response.status = 200
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    if failure == "timeout":
        opener.open.side_effect = TimeoutError("sensitive")
    elif failure == "http":
        opener.open.side_effect = urllib.error.HTTPError(
            "https://google.test", 503, "sensitive", {}, io.BytesIO(b"private")
        )
    elif failure == "redirect":
        opener.open.side_effect = lambda req, **kw: NoRedirect().redirect_request(
            req, None, 307, "redirect", {}, "https://evil.test"
        )
    else:
        response.read.return_value = (
            b"not json" if failure == "invalid_json" else b"x" * (artifact_http.MAX_BYTES + 1)
        )
        opener.open.return_value = response
    monkeypatch.setattr(artifact_http.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(PlayError, match="outcome unknown") as caught:
        service.apply(operation, operation)
    assert "sensitive" not in str(caught.value)
    opener.open.assert_called_once()
    assert operation not in service._preparations
