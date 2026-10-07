"""Immutable intent, local scoping and single-attempt failure behavior."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_appstore_contracts import APP, STORE, TARGET, P, case

from pubship import appstore_http
from pubship.appstore import AppStoreOperations
from pubship.appstore_config import AppStoreSettings
from pubship.appstore_contracts import (
    APK,
    APPSTORE_METHODS,
    CREATE,
    GET,
    IMAGE,
    LIST,
    POLICY,
)
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.publishing import _APPLY_LOCK


def configured(root=""):
    return AppStoreSettings(frozenset({STORE}), frozenset({APP}), APPSTORE_METHODS, root)


def service(root=""):
    auth = Mock()
    auth.token.return_value = "SYNTHETIC_PRIVATE_CREDENTIAL"
    return AppStoreOperations(configured(root), auth, local=True), auth


@pytest.mark.parametrize("method", sorted(APPSTORE_METHODS))
def test_all_methods_roundtrip_and_no_mutation_during_prepare(method, tmp_path, monkeypatch):
    p, body, mime, response = case(method)
    execute = Mock(return_value=response)
    monkeypatch.setattr(appstore_http, "execute", execute)
    svc, auth = service(str(tmp_path))
    (tmp_path / "upload.bin").write_bytes(b"original media")
    try:
        if method in {GET, LIST}:
            result = svc.query(method, p)
            assert result["data"] == response
        else:
            prepared = svc.prepare(method, p, body, True, "upload.bin" if mime else None, mime)
            assert not prepared["executed"] and not prepared["baseline_available"]
            execute.assert_not_called()
            operation = prepared["operation_id"]
            prepared["proposed_request"]["body"].clear()
            body.clear()
            p.clear()
            auth.token.return_value = "DIFFERENT_CREDENTIAL"
            (tmp_path / "upload.bin").write_bytes(b"changed bytes")
            result = svc.apply(operation, operation)
            assert result["mutation_succeeded"] and not result["readback_available"]
            assert not result["availability_verified"]
            assert execute.call_args.args[1] == "SYNTHETIC_PRIVATE_CREDENTIAL"
            assert execute.call_args.args[0]["parameters"]["appStorePackageName"] == STORE
            with pytest.raises(PlayError, match="consumed"):
                svc.apply(operation, operation)
        auth.token.assert_called_once_with("publisher")
        execute.assert_called_once()
    finally:
        svc.close()


def test_media_snapshot_is_immutable_and_closed(tmp_path, monkeypatch):
    svc, _ = service(str(tmp_path))
    path = tmp_path / "app.apk"
    path.write_bytes(b"original")
    operation = svc.prepare(APK, TARGET, {}, True, path.name, "application/octet-stream")[
        "operation_id"
    ]
    snapshot = svc._preparations[operation].snapshot
    path.write_bytes(b"replaced")

    def execute(spec, token, stored):
        assert stored.stream.read() == b"original"
        return {"apkId": "id-1"}

    monkeypatch.setattr(appstore_http, "execute", execute)
    svc.apply(operation, operation)
    assert snapshot.stream.closed
    svc.close()


@pytest.mark.parametrize(
    "change",
    [
        {"stores": frozenset()},
        {"packages": frozenset()},
        {"methods": frozenset()},
    ],
)
def test_scope_fails_before_auth(change):
    svc, auth = service()
    svc.settings = replace(svc.settings, **change)
    with pytest.raises(PlayError):
        svc.prepare(CREATE, P, {"packageName": APP}, True)
    auth.token.assert_not_called()


def test_hosted_disabled_acknowledgement_and_missing_files_fail_before_auth(tmp_path):
    svc, auth = service(str(tmp_path))
    with pytest.raises(PlayError):
        svc.prepare(CREATE, P, {"packageName": APP})
    for name in ("../private", "missing", ".hidden"):
        with pytest.raises(PlayError):
            svc.prepare(IMAGE, TARGET, {}, True, name, "image/png")
    svc.local = False
    with pytest.raises(PlayError):
        svc.query(GET, {**P, "playAppPackageName": APP})
    auth.token.assert_not_called()


def test_consumption_on_expiry_unknown_outcome_revoked_grant_and_lock(monkeypatch):
    execute = Mock(side_effect=PlayError(appstore_http.UNKNOWN_OUTCOME))
    monkeypatch.setattr(appstore_http, "execute", execute)
    svc, _ = service()
    for mode in ("expired", "unknown", "revoked", "locked"):
        svc.settings = configured()
        op = svc.prepare(CREATE, P, {"packageName": APP}, True)["operation_id"]
        if mode == "expired":
            svc._preparations[op].deadline = 0
        if mode == "revoked":
            svc.settings = replace(svc.settings, methods=frozenset())
        if mode == "locked":
            _APPLY_LOCK.acquire()
        try:
            with pytest.raises(PlayError):
                svc.apply(op, op)
        finally:
            if mode == "locked":
                _APPLY_LOCK.release()
        with pytest.raises(PlayError, match="consumed"):
            svc.apply(op, op)
    execute.assert_called_once()


def test_confirmation_failure_preserves_preparation(monkeypatch):
    svc, _ = service()
    monkeypatch.setattr(appstore_http, "execute", Mock(return_value={}))
    op = svc.prepare(CREATE, P, {"packageName": APP}, True)["operation_id"]
    with pytest.raises(PlayError, match="Confirmation"):
        svc.apply(op, "wrong")
    assert svc.apply(op, op)["executed"]


def test_capacity_prunes_and_closes_media(tmp_path, monkeypatch):
    svc, auth = service(str(tmp_path))
    (tmp_path / "file.pdf").write_bytes(b"document")
    monkeypatch.setattr("pubship.appstore.MAX_PREPARATIONS", 1)
    op = svc.prepare(
        POLICY,
        TARGET,
        {"fileType": "DECLARATION_FILE_TYPE_DOCUMENT"},
        True,
        "file.pdf",
        "application/pdf",
    )["operation_id"]
    p = svc._preparations[op]
    with pytest.raises(PlayError, match="capacity"):
        svc.prepare(CREATE, P, {"packageName": APP}, True)
    auth.token.assert_called_once()
    p.deadline = 0
    svc.prepare(CREATE, P, {"packageName": APP}, True)
    assert p.snapshot.stream.closed
    svc.close()


def test_config_requires_store_and_target_in_base_scope(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_STORES", STORE)
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_METHODS", CREATE)
    with pytest.raises(PlayError, match="within GOOGLE_PLAY_PACKAGES"):
        AppStoreSettings.from_env(Settings(packages=frozenset({STORE})))
    actual = AppStoreSettings.from_env(Settings(packages=frozenset({STORE, APP})))
    assert actual.methods == frozenset({CREATE})
    monkeypatch.setenv("GOOGLE_PLAY_APPSTORE_METHODS", "*")
    with pytest.raises(PlayError):
        AppStoreSettings.from_env(Settings(packages=frozenset({STORE, APP})))
