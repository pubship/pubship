"""Synthetic hosted media leases, original authority and lifecycle boundaries."""

import hashlib
import io
import json
import threading
import time
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_appstore_contracts import APP, STORE, case
from test_artifacts_engineering import external

from pubship import (
    appstore,
    appstore_http,
    artifact,
    artifact_http,
    provider_transport,
    sharing,
)
from pubship.appstore_config import AppStoreSettings
from pubship.appstore_contracts import APPSTORE_METHODS, POLICY, READ_METHODS
from pubship.artifact_contracts import CAPS, DOWNLOAD_METHODS, EXTERNAL, PREFIX
from pubship.artifact_files import Snapshot
from pubship.config import Settings
from pubship.coverage import ARTIFACT_METHODS, SHARING_METHODS
from pubship.errors import PlayError
from pubship.http import NoRedirect

PAYLOAD = b"PK\x03\x04synthetic-hosted-media\x00\xff"
TOKEN = "synthetic-private-original-token"
PARAMS = {"packageName": APP, "editId": "existing-edit"}


class Lease(Snapshot):
    def __init__(self, deadline=1450):
        super().__init__(
            io.BytesIO(PAYLOAD),
            len(PAYLOAD),
            hashlib.sha1(PAYLOAD).hexdigest(),
            hashlib.sha256(PAYLOAD).hexdigest(),
        )
        self.deadline = deadline
        self.status = "disposed"
        self.disposals = 0

    def dispose(self):
        self.disposals += 1
        if self.status == "disposed":
            self.stream.close()
        return self.status

    close = dispose


class Destination:
    def __init__(self, cap, deadline=1450):
        self.cap, self.deadline = cap, deadline
        self.size = 0
        self.payload = bytearray()
        self.published = False
        self.disposed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self.published:
            self.dispose()

    def write(self, chunk):
        assert self.size + len(chunk) <= self.cap
        self.payload.extend(chunk)
        self.size += len(chunk)

    def publish(self):
        assert self.size
        self.published = True
        return {
            "transfer_id": "opaque-download",
            "size": self.size,
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }

    def dispose(self):
        self.disposed = True
        return "disposed"


class Response(io.BytesIO):
    def __init__(self, payload, *, media=False, on_read=None):
        super().__init__(payload if media else json.dumps(payload).encode())
        self.status = 200
        self.headers = (
            {"Content-Type": "application/octet-stream", "Content-Length": str(len(payload))}
            if media
            else {}
        )
        self.on_read = on_read

    def read(self, size=-1):
        chunk = super().read(size)
        if self.on_read:
            self.on_read()
        return chunk


@pytest.fixture
def engines(monkeypatch):
    clock = [1000.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
    for module in (artifact, artifact_http, sharing, appstore, appstore_http, provider_transport):
        monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", Mock(side_effect=AssertionError("No real traffic"))
    )
    created = []

    def build(family, method=None):
        edit_expiry = str(int(time.time()) + 3600)
        state = SimpleNamespace(
            clock=clock, revoked=False, on_authorize=None, reads=[], writes=[], on_response=None
        )

        class Hook:
            deadline = 1500.0

            def __call__(self):
                if state.on_authorize:
                    state.on_authorize()
                if state.revoked:
                    raise PlayError("Synthetic authorization revoked")

        hook, lease = Hook(), Lease()
        source = Mock(return_value=lease)
        auth = Mock(token=Mock(return_value=TOKEN))
        guard = threading.Lock()
        options = dict(
            _execution_authorization=hook,
            _apply_guard=guard,
            _max_snapshot_bytes=256 * 1024,
            _snapshot_source=source,
        )
        state.family, state.hook, state.lease, state.source, state.auth, state.guard = (
            family,
            hook,
            lease,
            source,
            auth,
            guard,
        )
        if family == "appstore":
            method = method or POLICY
            settings = AppStoreSettings(
                frozenset({STORE}), frozenset({APP}), APPSTORE_METHODS, max_bytes=10 * 1024**3
            )
            svc = appstore.AppStoreOperations(settings, auth, **options)
            params, body, mime, mutation = case(method)
            state.prepare = lambda: svc.prepare(
                method,
                params,
                body,
                True,
                mime_type=mime,
                upload_handle="opaque-upload" if mime else None,
            )
        else:
            method = method or (
                PREFIX + "apks.upload" if family == "artifact" else sharing.PREFIX + "uploadapk"
            )
            settings = Settings(
                packages=frozenset({APP}),
                artifact_packages=frozenset({APP}),
                artifact_methods=ARTIFACT_METHODS | SHARING_METHODS,
                artifact_max_bytes=50 * 1024**3,
            )
            if family == "sharing":
                params, body, mime = {"packageName": APP}, None, "application/octet-stream"
                mutation = {"downloadUrl": "https://play.google.com/apps/test/synthetic"}
                svc = sharing.InternalSharing(settings, auth, **options)
                state.prepare = lambda: svc.prepare(
                    method, APP, acknowledge_sharing_effects=True, upload_handle="opaque-upload"
                )
            else:
                params, body, mime = dict(PARAMS), None, "application/octet-stream"
                mutation = {"versionCode": 7}
                if method.endswith("images.upload"):
                    params.update(language="en-US", imageType="icon")
                    mime, mutation = "image/png", {"image": {"id": "image1"}}
                elif method.endswith("deobfuscationfiles.upload"):
                    params.update(apkVersionCode=7, deobfuscationFileType="proguard")
                    mutation = {"deobfuscationFile": {"symbolType": "proguard"}}
                elif method.endswith("expansionfiles.upload"):
                    params.update(apkVersionCode=7, expansionFileType="main")
                    mutation = {"expansionFile": {"fileSize": str(len(PAYLOAD))}}
                elif method == EXTERNAL:
                    body, mime, mutation = (
                        external(),
                        None,
                        {"externallyHostedApk": {"packageName": APP, "versionCode": 7}},
                    )
                elif method in DOWNLOAD_METHODS:
                    mime = None
                    params = (
                        {"packageName": APP, "versionCode": 7, "downloadId": "opaque-provider-id"}
                        if method.endswith("generatedapks.download")
                        else {"packageName": APP, "versionCode": "7", "variantId": 1}
                    )
                destination = Destination(settings.artifact_max_bytes)
                state.destination = destination
                svc = artifact.Artifacts(
                    settings, auth, _destination_source=Mock(return_value=destination), **options
                )
                state.prepare = lambda: svc.prepare(
                    method,
                    params,
                    mime_type=mime,
                    body=body,
                    acknowledge_effects=True,
                    upload_handle="opaque-upload" if method in CAPS else None,
                )

        def get(url, token):
            state.reads.append((url, token))
            if url.endswith("/existing-edit"):
                return {"id": "existing-edit", "expiryTimeSeconds": edit_expiry}
            if url.endswith("/listings"):
                return {"listings": [{"language": "en-US"}]}
            if "/listings/" in url:
                return {"images": [{"id": "image1"}]}
            if "/expansionFiles/" in url:
                return {"fileSize": str(len(PAYLOAD))}
            if url.endswith("/bundles"):
                return {"bundles": []}
            return {"apks": [] if method == EXTERNAL else [{"versionCode": 7}]}

        monkeypatch.setattr(artifact.http, "get", get)

        def send(req, timeout):
            assert req.get_header("Authorization") == "Bearer " + TOKEN
            assert req.full_url.startswith("https://androidpublisher.googleapis.com/")
            state.writes.append(req)
            if method in DOWNLOAD_METHODS:
                return Response(PAYLOAD, media=True, on_read=state.on_response)
            if mime:
                chunks = []
                while chunk := req.data.read(7):
                    chunks.append(chunk)
                raw = b"".join(chunks)
                assert len(raw) == int(req.get_header("Content-length"))
                if method == POLICY:
                    message = BytesParser(policy=default).parsebytes(
                        (
                            "Content-Type: "
                            + req.get_header("Content-type")
                            + "\r\nMIME-Version: 1.0\r\n\r\n"
                        ).encode()
                        + raw
                    )
                    parts = list(message.iter_parts())
                    assert json.loads(parts[0].get_payload(decode=True)) == body
                    assert parts[1].get_payload(decode=True) == PAYLOAD
                else:
                    assert raw == PAYLOAD
            return Response(mutation, on_read=state.on_response)

        def opener(*handlers):
            assert len(handlers) == 3 and isinstance(handlers[0], NoRedirect)
            return Mock(handlers=[], open=send)

        monkeypatch.setattr("urllib.request.build_opener", opener)
        state.service, state.method, state.parameters, state.body, state.mime, state.mutation = (
            svc,
            method,
            params,
            body,
            mime,
            mutation,
        )
        created.append(state)
        return state

    yield build
    for state in created:
        state.lease.status = "disposed"
        state.service.close()
        state.lease.dispose()


METHODS = (
    [("artifact", m) for m in sorted(ARTIFACT_METHODS)]
    + [("sharing", m) for m in sorted(SHARING_METHODS)]
    + [("appstore", m) for m in sorted(APPSTORE_METHODS)]
)


@pytest.mark.parametrize("family,method", METHODS)
def test_all_eighteen_hosted_engine_paths_keep_fixed_requests_and_original_credentials(
    engines, family, method
):
    e = engines(family, method)
    if method in DOWNLOAD_METHODS:
        result = e.service.download(method, e.parameters)
        assert result["transfer_id"] == "opaque-download"
        assert "filename" not in result and e.destination.payload == PAYLOAD
        assert e.destination.published
    elif method in READ_METHODS:
        assert e.service.query(method, e.parameters)["data"] == e.mutation
    else:
        prepared = e.prepare()
        operation = prepared["operation_id"]
        retained = e.service._preparations[operation]
        assert retained.deadline == (1450 if e.mime else 1500)
        metadata = e.service._retained_metadata(operation)
        assert metadata["credential_bytes"] == len(TOKEN)
        assert TOKEN not in json.dumps(metadata)
        if e.mime:
            assert set(metadata["snapshot"]) == {"size", "sha1", "sha256"}
        assert not e.writes
        e.auth.token.return_value = "different-refreshed-credential"
        result = e.service.apply(operation, operation)
        assert result["mutation_succeeded"]
        assert len(e.writes) == 1
        assert e.service._dispose(operation) == "disposed"
        assert not e.service._preparations
        if e.mime:
            assert e.lease.stream.closed
    e.auth.token.assert_called_once_with("publisher")


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
@pytest.mark.parametrize("expiry", [1450, 1450.01])
def test_expiry_during_credential_acquisition_stops_reads_and_retention(engines, family, expiry):
    e = engines(family)
    e.auth.token.side_effect = lambda _: e.clock.__setitem__(0, expiry) or TOKEN
    with pytest.raises(PlayError, match="expired"):
        e.prepare()
    assert not e.reads and not e.writes and not e.service._preparations
    assert e.lease.stream.closed


@pytest.mark.parametrize("after_reads", [0, 1, 2])
@pytest.mark.parametrize("expiry", [1450, 1450.01])
def test_authorization_hook_expiry_blocks_each_artifact_get(engines, after_reads, expiry):
    e = engines("artifact", PREFIX + "images.upload")
    e.on_authorize = lambda: e.clock.__setitem__(0, expiry) if len(e.reads) == after_reads else None
    with pytest.raises(PlayError, match="expired"):
        e.prepare()
    assert len(e.reads) == after_reads
    assert not e.writes


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
def test_no_baseline_or_baseline_apply_cannot_dispatch_after_final_authority_hook(engines, family):
    e = engines(family)
    operation = e.prepare()["operation_id"]
    e.on_authorize = lambda: e.clock.__setitem__(0, 1450)
    with pytest.raises(PlayError, match="expired"):
        e.service.apply(operation, operation)
    assert not e.writes and e.lease.stream.closed


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
@pytest.mark.parametrize("status", ["cleanup-pending", "deferred-busy"])
def test_confirmed_write_survives_expiry_and_explicit_deferred_disposal(engines, family, status):
    e = engines(family)
    operation = e.prepare()["operation_id"]
    e.lease.status = status
    e.on_response = lambda: e.clock.__setitem__(0, 1600)
    result = e.service.apply(operation, operation)
    assert result["mutation_succeeded"] and "cleanup_warning" in result
    assert e.service._cleanup_pending
    assert not e.lease.stream.closed
    if family == "artifact":
        assert result["readback_succeeded"] is False
    assert e.guard.acquire(blocking=False)
    e.guard.release()
    e.lease.status = "disposed"
    assert e.service._dispose(operation) == "disposed"
    assert e.service._dispose(operation) == "disposed"
    assert e.lease.stream.closed and not e.service._cleanup_pending
    assert len(e.writes) == 1


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
def test_prepare_failure_disposal_failure_retains_reference_until_retry(engines, family):
    e = engines(family)
    e.lease.status = "cleanup-pending"
    e.auth.token.side_effect = PlayError("synthetic credential unavailable")
    with pytest.raises(PlayError, match="credential"):
        e.prepare()
    assert not e.service._preparations and e.service._cleanup_pending
    assert e.service._dispose("missing") == "cleanup-pending"
    e.lease.status = "disposed"
    assert e.service._dispose("missing") == "disposed"
    assert not e.service._cleanup_pending


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
def test_explicit_disposal_status_retains_reservation_even_when_descriptor_is_closed(
    engines, family
):
    e = engines(family)
    operation = e.prepare()["operation_id"]
    e.lease.stream.close()
    e.lease.status = "cleanup-pending"
    assert e.service._dispose(operation) == "cleanup-pending"
    assert e.service._cleanup_pending
    if family != "appstore":
        assert e.service._retained_bytes == len(PAYLOAD)
    e.lease.status = "disposed"
    assert e.service._dispose(operation) == "disposed"


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
def test_hosted_does_not_fall_back_to_configured_local_files(engines, family):
    e = engines(family)
    e.service._snapshot_source = None
    with pytest.raises(PlayError, match="snapshot source"):
        e.prepare()
    e.auth.token.assert_not_called()
    e.source.assert_not_called()


def test_artifact_observation_bound_counts_edit_and_prior_baselines_before_next_get(
    engines, monkeypatch
):
    e = engines("artifact", PREFIX + "images.upload")
    edit = {
        "id": "existing-edit",
        "expiryTimeSeconds": str(int(time.time()) + 3600),
        "extra": "a" * 100,
    }
    first = {"images": [], "extra": "b" * 100}
    e.service._max_snapshot_bytes = max(len(json.dumps(edit)), len(json.dumps(first))) + 1
    get = Mock(side_effect=[edit, first, AssertionError("No next baseline")])
    monkeypatch.setattr(artifact.http, "get", get)
    with pytest.raises(PlayError, match="snapshot limit"):
        e.prepare()
    assert get.call_count == 2
    assert e.lease.stream.closed and not e.writes


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
def test_apply_uses_injected_guard_and_consumes_when_busy(engines, family):
    e = engines(family)
    operation = e.prepare()["operation_id"]
    e.guard.acquire()
    try:
        with pytest.raises(PlayError, match="progress"):
            e.service.apply(operation, operation)
    finally:
        e.guard.release()
    assert e.lease.stream.closed and not e.writes


@pytest.mark.parametrize(
    "family,method,expected_cap",
    [
        ("artifact", PREFIX + "bundles.upload", 50 * 1024**3),
        ("sharing", sharing.PREFIX + "uploadbundle", 10 * 1024**3),
        ("appstore", "androidpublisher.appstoreappsreview.uploadapk", 10 * 1024**3),
    ],
)
def test_large_permitted_configuration_reaches_source_without_giant_allocation(
    engines, family, method, expected_cap
):
    e = engines(family, method)
    e.prepare()
    assert e.source.call_args.args == ("opaque-upload", expected_cap)


@pytest.mark.parametrize("family", ["artifact", "sharing", "appstore"])
def test_snapshot_cannot_exceed_trusted_source_cap(engines, family):
    e = engines(family)
    e.lease.size = 60 * 1024**3
    with pytest.raises(PlayError, match="byte limit"):
        e.prepare()
    e.auth.token.assert_not_called()
    assert e.lease.stream.closed


@pytest.mark.parametrize("family", ["artifact", "appstore"])
def test_authorization_loss_during_upload_read_sends_no_next_chunk_or_retry(
    engines, monkeypatch, family
):
    e = engines(family)
    operation = e.prepare()["operation_id"]
    read = e.lease.stream.read

    def revoked_read(amount):
        chunk = read(amount)
        e.revoked = True
        return chunk

    monkeypatch.setattr(e.lease.stream, "read", revoked_read)
    with pytest.raises(PlayError, match="outcome unknown"):
        e.service.apply(operation, operation)
    assert len(e.writes) == 1 and e.lease.stream.closed


@pytest.mark.parametrize("method", sorted(DOWNLOAD_METHODS))
def test_expired_download_cannot_write_or_publish_received_chunk(engines, method):
    e = engines("artifact", method)
    e.on_response = lambda: e.clock.__setitem__(0, 1450)
    with pytest.raises(PlayError, match="download failed"):
        e.service.download(method, e.parameters)
    assert not e.destination.payload and not e.destination.published
    assert e.destination.disposed and len(e.writes) == 1


@pytest.mark.parametrize("method", sorted(READ_METHODS))
def test_appstore_query_checks_authority_after_credential_before_dispatch(engines, method):
    e = engines("appstore", method)
    e.auth.token.side_effect = lambda _: setattr(e, "revoked", True) or TOKEN
    with pytest.raises(PlayError, match="revoked"):
        e.service.query(method, e.parameters)
    assert not e.writes
