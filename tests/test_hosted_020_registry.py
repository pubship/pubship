"""Actual transfer-store integration with current OAuth and fixed synthetic providers."""

import asyncio
import atexit
import hashlib
import io
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_appstore_contracts import APP, STORE, case
from test_artifacts_engineering import external
from test_hosted_grants import issue
from test_hosted_grants import provider as provider  # noqa: F401

from pubship import artifact
from pubship.appstore_contracts import GET, LIST, POLICY, READ_METHODS
from pubship.artifact_contracts import DOWNLOAD_METHODS, EXTERNAL, PREFIX
from pubship.errors import PlayError
from pubship.hosted_catalog import (
    HOSTED_APPSTORE_CAPABILITIES,
    HOSTED_ARTIFACT_CAPABILITIES,
    HOSTED_SHARING_CAPABILITIES,
)
from pubship.hosted_config import TransferLimits
from pubship.hosted_edits import HostedEditRegistry
from pubship.hosted_grants import APPSTORE_PAIR_SCOPES, READ_SCOPE, grant_authority
from pubship.hosted_transfers import HostedTransferStore

DATA = b"PK\x03\x04synthetic registry binary"
OTHER = "com.example.other"
OTHER_STORE = "com.example.otherstore"
METHODS = {
    **HOSTED_ARTIFACT_CAPABILITIES,
    **HOSTED_SHARING_CAPABILITIES,
    **HOSTED_APPSTORE_CAPABILITIES,
}


def connection(provider, subject="one", client="client-one", *, store_only=False):
    grant, _, _ = issue(provider, subject, client)
    grant.update(
        capability_version=2,
        packages=[] if store_only else [APP],
        capability_packages={READ_SCOPE: [] if store_only else [APP]},
        appstore_targets={
            cap: {STORE: [APP], OTHER_STORE: [OTHER]} for cap in APPSTORE_PAIR_SCOPES
        },
        appstore_event_stores=[STORE],
    )
    if not store_only:
        grant["capability_packages"].update(
            {
                cap: [APP]
                for cap in {
                    "play:edit-artifacts",
                    "play:artifact-downloads",
                    "play:internal-sharing",
                    "play:edit-listings",
                }
            }
        )
    provider.vault.put("grants", subject, grant, 3600)
    tokens = provider.issue_tokens(subject, client, sorted(grant_authority(grant).scopes))
    return asyncio.run(provider.load_access_token(tokens.access_token))


@pytest.fixture
def context(provider, tmp_path, monkeypatch):
    root = tmp_path / "transfers"
    root.mkdir(mode=0o700)
    store = HostedTransferStore(
        root,
        TransferLimits(object_bytes=8192, connection_bytes=16384, total_bytes=32768),
        _allocate=lambda fd, offset, size: os.ftruncate(fd, size),
    )
    provider.register_invalidator(store.invalidate)
    registry = HostedEditRegistry(provider, transfers=store)
    principal = connection(provider)
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", Mock(side_effect=AssertionError("No real network"))
    )
    yield SimpleNamespace(provider=provider, store=store, registry=registry, principal=principal)
    registry.close()
    store.close()


def scenario(monkeypatch, method):
    state = SimpleNamespace(method=method, gets=[], requests=[])
    if method in HOSTED_APPSTORE_CAPABILITIES:
        state.kind = "appstore"
        parameters, body, mime, result = case(method)
    elif method in HOSTED_SHARING_CAPABILITIES:
        state.kind = "sharing"
        parameters, body, mime, result = (
            {"packageName": APP},
            None,
            "application/octet-stream",
            {"downloadUrl": "https://play.google.com/apps/test/synthetic"},
        )
    else:
        state.kind = "artifact"
        parameters, body, mime, result = (
            {"packageName": APP, "editId": "edit1"},
            None,
            "application/octet-stream",
            {"versionCode": 7},
        )
        if method.endswith("images.upload"):
            parameters.update(language="en-US", imageType="icon")
            mime, result = "image/png", {"image": {"id": "image1"}}
        elif method.endswith("deobfuscationfiles.upload"):
            parameters.update(apkVersionCode=7, deobfuscationFileType="proguard")
            result = {"deobfuscationFile": {"symbolType": "proguard"}}
        elif method.endswith("expansionfiles.upload"):
            parameters.update(apkVersionCode=7, expansionFileType="main")
            result = {"expansionFile": {"fileSize": str(len(DATA))}}
        elif method == EXTERNAL:
            body, mime = external(), None
            result = {"externallyHostedApk": {"packageName": APP, "versionCode": 7}}
        elif method in DOWNLOAD_METHODS:
            mime = None
            parameters = (
                {"packageName": APP, "versionCode": 7, "downloadId": "opaque-download"}
                if method.endswith("generatedapks.download")
                else {"packageName": APP, "versionCode": "7", "variantId": 1}
            )

    def get(url, token):
        state.gets.append((url, token))
        if url.endswith("/edit1"):
            return {"id": "edit1", "expiryTimeSeconds": str(int(time.time()) + 3600)}
        if url.endswith("/listings"):
            return {"listings": [{"language": "en-US"}]}
        if "/listings/" in url:
            return {"images": [{"id": "image1"}]}
        if "/expansionFiles/" in url:
            return {"fileSize": str(len(DATA))}
        if url.endswith("/bundles"):
            return {"bundles": []}
        return {"apks": [] if method == EXTERNAL else [{"versionCode": 7}]}

    def send(req, timeout):
        state.requests.append(req)
        assert req.get_header("Authorization") == "Bearer synthetic-google-access"
        assert req.full_url.startswith("https://androidpublisher.googleapis.com/")
        if mime:
            content = bytearray()
            while chunk := req.data.read(5):
                content.extend(chunk)
            assert len(content) == int(req.get_header("Content-length"))
            assert DATA in content
            if method == POLICY:
                assert b"DECLARATION_FILE_TYPE_DOCUMENT" in content
        response = io.BytesIO(DATA if method in DOWNLOAD_METHODS else json.dumps(result).encode())
        response.status = 200
        response.headers = (
            {"Content-Type": "application/octet-stream", "Content-Length": str(len(DATA))}
            if method in DOWNLOAD_METHODS
            else {}
        )
        return response

    monkeypatch.setattr(artifact.http, "get", get)
    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Mock(handlers=[], open=send))
    state.parameters, state.body, state.mime, state.result = parameters, body, mime, result
    return state


def upload(context, state, principal=None):
    principal = principal or context.principal
    reply = context.registry.begin_upload_transfer(
        principal,
        state.method,
        state.parameters,
        state.mime,
        len(DATA),
        hashlib.sha256(DATA).hexdigest(),
        state.body,
    )
    handle = reply["transfer_id"]
    lease = context.store.begin_receive(
        (principal.subject, principal.client_id),
        handle,
        authorize=lambda: context.provider.authorize_current(principal),
    )
    lease.write(DATA)
    lease.seal()
    return handle


def prepare(context, state, principal=None, handle=None):
    principal = principal or context.principal
    if state.mime and handle is None:
        handle = upload(context, state, principal)
    return context.registry.prepare(
        principal,
        state.kind,
        state.parameters,
        state.body,
        True,
        method=state.method,
        upload_handle=handle,
        mime_type=state.mime,
    )


@pytest.mark.parametrize("method", sorted(METHODS))
def test_all_eighteen_registry_paths_use_actual_store_and_current_oauth(
    context, monkeypatch, method
):
    state = scenario(monkeypatch, method)
    if method in DOWNLOAD_METHODS:
        result = context.registry.download_artifact(context.principal, method, state.parameters)
        assert "filename" not in result
        transfer = result["transfer_id"]
        status = context.registry.get_transfer_status(context.principal, transfer)
        assert status["state"] == "DOWNLOAD_READY"
        reader = context.store.open_download(
            (context.principal.subject, context.principal.client_id),
            transfer,
            authorize=lambda: context.provider.authorize_current(context.principal),
        )
        assert reader.read() == DATA
        reader.close()
        assert (
            context.registry.discard_transfer(context.principal, transfer)["disposal_status"]
            == "disposed"
        )
    elif method in READ_METHODS:
        assert (
            context.registry.query_appstore(context.principal, method, state.parameters)["data"]
            == state.result
        )
    else:
        operation = prepare(context, state)["operation_id"]
        record = context.registry._records[operation]
        assert record.capability == METHODS[method]
        assert record.engine._apply_guard is record.guard
        assert record.retained_bytes < 512 * 1024
        assert record.engine._execution_authorization.principal is None
        with pytest.raises(PlayError):
            context.registry.apply(context.principal, "wrong-family", operation, operation)
        assert operation in context.registry._records
        result = context.registry.apply(context.principal, state.kind, operation, operation)
        assert result["mutation_succeeded"] and result["operation_id"] == operation
        assert len(state.requests) == 1
    assert not context.registry._records and not context.store._records


@pytest.mark.parametrize("method", [POLICY, GET, LIST])
def test_store_only_connections_never_require_or_acquire_base_app_reads(
    context, monkeypatch, method
):
    principal = connection(context.provider, "store-only", "store-client", store_only=True)
    state = scenario(monkeypatch, method)
    if method in READ_METHODS:
        result = context.registry.query_appstore(principal, method, state.parameters)
        assert result["data"] == state.result
    else:
        operation = prepare(context, state, principal)["operation_id"]
        assert context.registry.apply(principal, "appstore", operation, operation)[
            "mutation_succeeded"
        ]
    with pytest.raises(PlayError):
        context.provider.authorize_current(principal, READ_SCOPE, APP)


@pytest.mark.parametrize("mode", ["client", "connection", "target", "capability"])
def test_foreign_or_wrong_purpose_access_preserves_unconsumed_upload(context, monkeypatch, mode):
    state = scenario(monkeypatch, POLICY)
    handle = upload(context, state)
    principal = context.principal
    if mode == "client":
        principal = principal.model_copy(update={"client_id": "other-client"})
    elif mode == "connection":
        principal = connection(context.provider, "other", "other-client")
    elif mode == "target":
        state.parameters["packageName"] = OTHER
    else:
        grant = context.provider.vault.get("grants", principal.subject)
        grant["appstore_targets"].pop("play:appstore-media")
        context.provider.vault.put("grants", principal.subject, grant, 3600)
    with pytest.raises(PlayError):
        prepare(context, state, principal, handle)
    assert (
        context.store.status((context.principal.subject, context.principal.client_id), handle)[
            "state"
        ]
        == "READY"
    )
    assert not state.requests and not state.gets and not context.registry._records


@pytest.mark.parametrize("field", ["packageName", "appStorePackageName"])
def test_independent_pair_grants_do_not_authorize_cross_product(context, monkeypatch, field):
    state = scenario(monkeypatch, POLICY)
    state.parameters[field] = OTHER if field == "packageName" else OTHER_STORE
    with pytest.raises(PlayError, match="targets"):
        upload(context, state)
    assert not context.store._records and not state.requests


def test_policy_reservation_requires_actual_metadata_without_provider_access(context, monkeypatch):
    state = scenario(monkeypatch, POLICY)
    state.body = None
    with pytest.raises(PlayError):
        upload(context, state)
    assert not context.store._records and not context.registry._records and not state.requests


@pytest.mark.parametrize("action", ["expiry", "disconnect", "close"])
def test_cleanup_disposes_store_leases_outside_registry_lock(context, monkeypatch, action):
    state = scenario(monkeypatch, POLICY)
    operation = prepare(context, state)["operation_id"]
    record = context.registry._records[operation]
    engine = record.engine
    close = engine.close

    def checked_close():
        assert context.registry._lock.acquire(blocking=False)
        context.registry._lock.release()
        close()

    monkeypatch.setattr(engine, "close", checked_close)
    if action == "expiry":
        record.deadline = 0
        context.registry.cleanup()
    elif action == "disconnect":
        context.provider.disconnect(context.principal.subject)
    else:
        context.registry.close()
    assert not context.registry._records and not context.store._records
    assert not engine._preparations and engine._execution_authorization.principal is None


def test_pending_disposal_retains_both_quotas_and_cannot_be_applied_again(context, monkeypatch):
    state = scenario(monkeypatch, POLICY)
    operation = prepare(context, state)["operation_id"]
    record = context.registry._records[operation]
    lease = record.engine._preparations[record.inner_id].snapshot
    dispose = lease.dispose
    monkeypatch.setattr(lease, "dispose", lambda: "cleanup-pending")
    result = context.registry.apply(context.principal, "appstore", operation, operation)
    assert result["mutation_succeeded"] and result["cleanup_warning"]
    assert context.registry._records[operation].disposal_pending
    assert all(p.token == "" for p in record.engine._cleanup_pending)
    assert context.store._records
    context.registry._limits = (1, 1, 1024**2, 1024**2)
    with pytest.raises(PlayError, match="capacity"):
        context.registry.prepare(
            context.principal,
            "artifact",
            {"packageName": APP, "editId": "edit1"},
            external(),
            True,
            method=EXTERNAL,
        )
    with pytest.raises(PlayError, match="consumed"):
        context.registry.apply(context.principal, "appstore", operation, operation)
    monkeypatch.setattr(lease, "dispose", dispose)
    context.registry.cleanup()
    assert not context.registry._records and not context.store._records
    assert len(state.requests) == 1


def test_repeated_hosted_operations_unregister_engines_and_transient_reads(context, monkeypatch):
    registered = set()
    monkeypatch.setattr(atexit, "register", lambda callback: registered.add(callback))
    monkeypatch.setattr(atexit, "unregister", lambda callback: registered.discard(callback))
    for _ in range(8):
        state = scenario(monkeypatch, POLICY)
        operation = prepare(context, state)["operation_id"]
        assert len(registered) == 1
        context.registry.apply(context.principal, "appstore", operation, operation)
        assert not registered
        state = scenario(monkeypatch, GET)
        context.registry.query_appstore(context.principal, GET, state.parameters)
        assert not registered
    state = scenario(monkeypatch, POLICY)
    handle = upload(context, state)
    with pytest.raises(PlayError, match="acknowledge"):
        context.registry.prepare(
            context.principal,
            "appstore",
            state.parameters,
            state.body,
            False,
            method=POLICY,
            upload_handle=handle,
            mime_type=state.mime,
        )
    assert not registered and not context.registry._records


def test_shared_guard_covers_distribution_and_existing_listing_family(context, monkeypatch):
    state = scenario(monkeypatch, POLICY)
    operation = prepare(context, state)["operation_id"]
    record = context.registry._records[operation]
    monkeypatch.setattr(
        artifact.http,
        "get",
        lambda url, token: (
            {"language": "en-US", "title": "old"}
            if "/listings/" in url
            else {"id": "edit1", "expiryTimeSeconds": str(int(time.time()) + 3600)}
        ),
    )
    listing = context.registry.prepare(
        context.principal,
        "listing",
        {"packageName": APP, "editId": "edit1", "language": "en-US"},
        {"title": "new"},
    )["operation_id"]
    assert context.registry._records[listing].guard is record.guard
    record.guard.acquire()
    try:
        with pytest.raises(PlayError, match="progress"):
            context.registry.apply(context.principal, "appstore", operation, operation)
    finally:
        record.guard.release()
    assert not state.requests and not context.store._records
    assert set(context.registry._records) == {listing}
    context.registry._records[listing].deadline = 0
    context.registry.cleanup()
    assert not context.registry._records


def test_unknown_extra_distribution_arguments_fail_before_credentials(context, monkeypatch):
    services = Mock(side_effect=AssertionError("No credentials"))
    monkeypatch.setattr(context.provider, "services", services)
    with pytest.raises(PlayError):
        context.registry.prepare(
            context.principal, "appstore", {}, {}, True, action="commit", method=POLICY
        )
    with pytest.raises(PlayError):
        context.registry.prepare(context.principal, "listing", {}, {}, upload_handle="foreign")
    services.assert_not_called()


@pytest.mark.parametrize("event", ["expire", "revoke", "close"])
def test_authority_loss_between_artifact_reads_disposes_claimed_upload(context, monkeypatch, event):
    state = scenario(monkeypatch, PREFIX + "images.upload")
    handle = upload(context, state)
    original_get = artifact.http.get

    def get(url, token):
        result = original_get(url, token)
        if len(state.gets) == 1:
            if event == "expire":
                next(
                    iter(context.registry._records.values())
                ).engine._execution_authorization.deadline = 0
            elif event == "revoke":
                grant = context.provider.vault.get("grants", context.principal.subject)
                grant["capability_packages"].pop("play:edit-artifacts")
                context.provider.vault.put("grants", context.principal.subject, grant, 3600)
            else:
                context.registry.close()
                # In-flight preparations retain their quota until their finally runs.
                assert context.registry._records
        return result

    monkeypatch.setattr(artifact.http, "get", get)
    with pytest.raises(PlayError):
        prepare(context, state, handle=handle)
    assert len(state.gets) == 1 and not state.requests
    assert not context.registry._records and not context.store._records


def test_known_write_survives_registry_engine_close_failure(context, monkeypatch):
    state = scenario(monkeypatch, POLICY)
    operation = prepare(context, state)["operation_id"]
    engine = context.registry._records[operation].engine
    close = engine.close
    monkeypatch.setattr(engine, "close", Mock(side_effect=OSError("PRIVATE path")))
    result = context.registry.apply(context.principal, "appstore", operation, operation)
    assert result["mutation_succeeded"] and result["cleanup_warning"]
    assert "PRIVATE" not in str(result)
    assert context.registry._records and not context.store._records
    monkeypatch.setattr(engine, "close", close)
    context.registry.cleanup()
    assert not context.registry._records and len(state.requests) == 1
