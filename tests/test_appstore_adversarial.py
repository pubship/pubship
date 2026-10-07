"""Independent app-store boundary tests using synthetic files, credentials and transport."""

import io
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_appstore_contracts import APP, TARGET, P, case
from test_appstore_service import service
from test_appstore_transport import Response

from pubship import appstore_http
from pubship.appstore_contracts import APK, CREATE, GET, LIST, request_spec
from pubship.errors import PlayError


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", Mock(side_effect=AssertionError("No live traffic"))
    )


def test_cross_store_and_target_grants_fail_before_credentials():
    svc, auth = service()
    try:
        for params, body in (
            ({"appStorePackageName": "com.other.store"}, {"packageName": APP}),
            (P, {"packageName": "com.other.app"}),
        ):
            with pytest.raises(PlayError):
                svc.prepare(CREATE, params, body, True)
        auth.token.assert_not_called()
    finally:
        svc.close()


def test_store_wide_list_does_not_expand_individual_app_view_authority(monkeypatch):
    svc, auth = service()
    outside = "com.other.app"
    params, _, _, _ = case(LIST)
    response = {
        "recentUpdateEvents": [
            {
                "playAppPackageName": outside,
                "eventTime": params["startTime"],
                "updateType": "MODIFICATION",
            }
        ],
        "nextPageToken": "opaque",
    }
    send = Mock(return_value=response)
    monkeypatch.setattr(appstore_http, "execute", send)
    try:
        result = svc.query(LIST, params)
        assert result["data"]["recentUpdateEvents"][0]["playAppPackageName"] == outside
        assert result["data"]["nextPageToken"] == "opaque"
        with pytest.raises(PlayError):
            svc.query(GET, {**P, "playAppPackageName": outside})
        assert auth.token.call_count == send.call_count == 1
    finally:
        svc.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "empty"])
def test_unsafe_source_forms_rejected_before_auth(tmp_path, kind):
    source = tmp_path / "upload.apk"
    other = tmp_path / "source.apk"
    other.write_bytes(b"private source")
    if kind == "symlink":
        source.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, source)
    elif kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        os.mkfifo(source)
    else:
        source.write_bytes(b"")
    svc, auth = service(str(tmp_path))
    try:
        with pytest.raises(PlayError):
            svc.prepare(APK, TARGET, {}, True, source.name, "application/octet-stream")
        auth.token.assert_not_called()
        assert not svc._preparations
    finally:
        svc.close()


def test_auth_failure_and_auth_expiry_close_private_snapshot(tmp_path, monkeypatch):
    (tmp_path / "upload.apk").write_bytes(b"private file")
    svc, auth = service(str(tmp_path))
    actual = svc.files.snapshot
    retained = []

    def capture(*args):
        snapshot = actual(*args)
        retained.append(snapshot)
        return snapshot

    monkeypatch.setattr(svc.files, "snapshot", capture)
    try:
        auth.token.side_effect = PlayError("Authentication unavailable.")
        with pytest.raises(PlayError):
            svc.prepare(APK, TARGET, {}, True, "upload.apk", "application/octet-stream")
        assert retained[-1].stream.closed
        clock = [0]
        monkeypatch.setattr("pubship.appstore.time.monotonic", lambda: clock[0])

        def late(*args):
            clock[0] = 601
            return "synthetic-token"

        auth.token.side_effect = late
        with pytest.raises(PlayError, match="expired"):
            svc.prepare(APK, TARGET, {}, True, "upload.apk", "application/octet-stream")
        assert retained[-1].stream.closed
        assert not svc._preparations
    finally:
        svc.close()


def test_concurrent_confirmation_consumes_once_and_keeps_credentials(monkeypatch):
    svc, auth = service()
    sent = Mock(return_value={})
    monkeypatch.setattr(appstore_http, "execute", sent)
    op = svc.prepare(CREATE, P, {"packageName": APP}, True)["operation_id"]
    auth.token.return_value = "another-account-token"

    def attempt():
        try:
            return svc.apply(op, op)["mutation_succeeded"]
        except PlayError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        assert sorted(results) == [False, True]
        assert sent.call_count == auth.token.call_count == 1
        assert sent.call_args.args[1] == "SYNTHETIC_PRIVATE_CREDENTIAL"
    finally:
        svc.close()


def test_operation_id_from_other_process_service_cannot_be_used(monkeypatch):
    first, _ = service()
    second, auth = service()
    send = Mock(return_value={})
    monkeypatch.setattr(appstore_http, "execute", send)
    try:
        op = first.prepare(CREATE, P, {"packageName": APP}, True)["operation_id"]
        with pytest.raises(PlayError, match="Unknown"):
            second.apply(op, op)
        assert op in first._preparations
        auth.token.assert_not_called()
        send.assert_not_called()
    finally:
        first.close()
        second.close()


def test_revoked_upload_scope_still_closes_snapshot(tmp_path, monkeypatch):
    (tmp_path / "upload.apk").write_bytes(b"private file")
    svc, _ = service(str(tmp_path))
    send = Mock()
    monkeypatch.setattr(appstore_http, "execute", send)
    try:
        op = svc.prepare(APK, TARGET, {}, True, "upload.apk", "application/octet-stream")[
            "operation_id"
        ]
        snapshot = svc._preparations[op].snapshot
        svc.settings = replace(svc.settings, stores=frozenset())
        with pytest.raises(PlayError):
            svc.apply(op, op)
        assert snapshot.stream.closed
        send.assert_not_called()
    finally:
        svc.close()


@pytest.mark.parametrize("data,size", [(b"truncated", 100), (b"overflow", 1)])
def test_media_size_mismatch_is_unknown(data, size):
    params, body, mime, _ = case(APK)
    stream = appstore_http.MediaBody(
        request_spec(APK, params, body, mime), SimpleNamespace(stream=io.BytesIO(data), size=size)
    )
    with pytest.raises(PlayError, match="unknown"):
        while stream.read(2):
            pass


def test_transfer_deadline_is_enforced_without_file_contents(monkeypatch):
    params, body, mime, _ = case(APK)
    stream = appstore_http.MediaBody(
        request_spec(APK, params, body, mime), SimpleNamespace(stream=io.BytesIO(b"secret"), size=6)
    )
    stream.deadline = 0
    with pytest.raises(PlayError, match="unknown") as error:
        stream.read()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "payload", [b"null", b"[]", b'{"apkId":null}', b'{"apkId":""}', b'{"apkId":NaN}']
)
def test_upload_invalid_response_is_unknown_no_retry(payload, monkeypatch):
    params, body, mime, _ = case(APK)
    send = Mock(return_value=Response(payload))
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Mock(handlers=[], open=send))
    snapshot = SimpleNamespace(stream=io.BytesIO(b"file"), size=4)
    with pytest.raises(PlayError, match="unknown"):
        appstore_http.execute(request_spec(APK, params, body, mime), "synthetic", snapshot)
    assert send.call_count == 1
