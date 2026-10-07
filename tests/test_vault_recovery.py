"""Synthetic reconnect-only recovery; never read production credentials."""

import asyncio
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient
from test_hosted import begin, call_tool, callback, connect_account, exchange, hosted  # noqa: F401

from pubship.errors import PlayError
from pubship.hosted import create_hosted_app
from pubship.vault import Vault
from pubship.vault_recovery import backup, main, recover


@pytest.fixture
def state(tmp_path):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    key = Fernet.generate_key()
    key_file = tmp_path / "key"
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    source = tmp_path / "source.db"
    vault = Vault(source, key)
    vault.put("grants", "synthetic-family", {"client_id": "synthetic-client"}, 600)
    vault.put("access", "synthetic-access", {"subject": "synthetic-family"}, 600)
    try:
        yield tmp_path, source, key_file, key, vault
    finally:
        vault.close()


def assert_no_output(directory, target):
    assert not target.exists()
    assert not list(directory.glob(".vault-recovery-*"))
    assert not list(directory.glob(target.name + "-*"))


def test_backup_captures_wal_and_recovery_is_empty_and_source_unchanged(state):
    directory, source, key_file, key, vault = state
    assert Path(str(source) + "-wal").stat().st_size > 0
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    before_source = source.read_bytes()
    before_wal = Path(str(source) + "-wal").read_bytes()
    backup(source, snapshot, key_file)
    assert source.read_bytes() == before_source
    assert Path(str(source) + "-wal").read_bytes() == before_wal
    assert snapshot.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(snapshot)) as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 2
    assert b"synthetic-client" not in snapshot.read_bytes()
    before = snapshot.read_bytes()
    recover(snapshot, target, key_file)
    assert snapshot.read_bytes() == before
    assert target.stat().st_mode & 0o777 == 0o600
    with closing(Vault(target, key)) as restored:
        assert restored.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
    assert vault.get("access", "synthetic-access") is not None


@pytest.mark.parametrize("operation", [backup, recover])
def test_wrong_key_fails_without_output(state, operation):
    directory, source, key_file, _, _ = state
    snapshot = directory / "backup.db"
    backup(source, snapshot, key_file)
    key_file.write_bytes(Fernet.generate_key())
    target = directory / "recovered.db"
    with pytest.raises(PlayError):
        operation(source if operation is backup else snapshot, target, key_file)
    assert_no_output(directory, target)


def test_empty_snapshot_still_verifies_key(state):
    directory, source, key_file, _, vault = state
    vault.delete_connection("synthetic-family")
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    backup(source, snapshot, key_file)
    key_file.write_bytes(Fernet.generate_key())
    with pytest.raises(PlayError):
        recover(snapshot, target, key_file)
    assert_no_output(directory, target)


@pytest.mark.parametrize(
    "corruption", ["ciphertext", "expired", "identity", "expiry", "sqlite", "marker"]
)
def test_corruption_fails_without_output(state, corruption):
    directory, source, key_file, _, _ = state
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    backup(source, snapshot, key_file)
    if corruption == "sqlite":
        snapshot.write_bytes(b"not a database")
    else:
        with closing(sqlite3.connect(snapshot)) as db, db:
            if corruption in {"ciphertext", "expired"}:
                db.execute("UPDATE records SET payload=?", (b"broken",))
                if corruption == "expired":
                    db.execute("UPDATE records SET expires=1")
            elif corruption == "identity":
                db.execute("UPDATE records SET key=?", ("a" * 64,))
            elif corruption == "expiry":
                db.execute("UPDATE records SET expires=expires+1")
            else:
                db.execute("DELETE FROM vault_backup_metadata")
    before = snapshot.read_bytes()
    with pytest.raises(PlayError):
        recover(snapshot, target, key_file)
    assert snapshot.read_bytes() == before
    assert_no_output(directory, target)


@pytest.mark.parametrize("operation", [backup, recover])
@pytest.mark.parametrize(
    "unsafe",
    [
        "same",
        "existing",
        "source_link",
        "target_link",
        "key_link",
        "directory_link",
        "source_mode",
        "key_mode",
        "directory_mode",
        "hardlink",
        "sidecar",
        "fifo",
    ],
)
def test_unsafe_paths_rejected_without_overwrite(state, operation, unsafe):
    directory, source, key_file, _, _ = state
    snapshot = directory / "backup.db"
    backup(source, snapshot, key_file)
    source = source if operation is backup else snapshot
    target = directory / "recovered.db"
    original = source.read_bytes()
    if unsafe == "same":
        target = source
    elif unsafe == "existing":
        target.write_bytes(b"existing data")
        target.chmod(0o600)
    elif unsafe in {"source_link", "target_link", "key_link"}:
        link = directory / "link"
        link.symlink_to(key_file if unsafe == "key_link" else source)
        if unsafe == "source_link":
            source = link
        elif unsafe == "target_link":
            target = link
        else:
            key_file = link
    elif unsafe == "directory_link":
        (directory / "alias").symlink_to(directory, target_is_directory=True)
        target = directory / "alias" / target.name
    elif unsafe == "source_mode":
        source.chmod(0o644)
    elif unsafe == "key_mode":
        key_file.chmod(0o644)
    elif unsafe == "directory_mode":
        directory.chmod(0o755)
    elif unsafe == "hardlink":
        os.link(source, directory / "other")
    elif unsafe == "sidecar":
        Path(str(target) + "-wal").write_bytes(b"existing sidecar")
    elif unsafe == "fifo":
        source = directory / "fifo"
        os.mkfifo(source, 0o600)
    with pytest.raises(PlayError):
        operation(source, target, key_file)
    if unsafe == "existing":
        assert target.read_bytes() == b"existing data"
    if unsafe == "same":
        assert target.read_bytes() == original
    assert not list(directory.glob(".vault-recovery-*"))


@pytest.mark.parametrize("operation", [backup, recover])
@pytest.mark.parametrize(
    "failure", ["link", "fsync", "space", "owner", "bytes", "records", "deadline"]
)
def test_failures_cleanup_without_output(state, monkeypatch, operation, failure):
    from pubship import vault_recovery as recovery

    directory, source, key_file, _, _ = state
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    backup(source, snapshot, key_file)

    def denied(*args, **kwargs):
        raise OSError("synthetic private failure details")

    if failure in {"link", "fsync"}:
        monkeypatch.setattr(recovery.os, failure, denied)
    elif failure == "space":
        monkeypatch.setattr(recovery.tempfile, "mkstemp", denied)
    elif failure == "owner":
        monkeypatch.setattr(recovery.os, "getuid", lambda: 987654)
    elif failure == "bytes":
        monkeypatch.setattr(recovery, "MAX_BYTES", 1)
    elif failure == "records":
        monkeypatch.setattr(recovery, "MAX_RECORDS", 0)
    else:
        monkeypatch.setattr(recovery, "MAX_SECONDS", 0)
    with pytest.raises(PlayError) as error:
        operation(source if operation is backup else snapshot, target, key_file)
    assert "synthetic private" not in str(error.value)
    assert_no_output(directory, target)


def test_cli_redacts_failure(state, capsys):
    directory, source, key_file, _, _ = state
    key_file.write_bytes(Fernet.generate_key())
    target = directory / "output.db"
    assert (
        main(
            [
                "backup",
                "--source",
                str(source),
                "--target",
                str(target),
                "--key-file",
                str(key_file),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert str(directory) not in output.err
    assert_no_output(directory, target)


def test_recovery_invalidates_old_http_authority_and_fresh_connection_survives_restart(hosted):  # noqa: F811
    settings, app, client = hosted
    directory = settings.database.parent.resolve()
    key_file = directory / "recovery-key"
    key_file.write_bytes(settings.encryption_key)
    key_file.chmod(0o600)
    old_request, old = connect_account(client, settings, "com.example.alice", "SYNTHETIC-ALICE")
    pending = begin(client, settings, "com.example.pending")
    old_code = callback(client, pending, "SYNTHETIC-PENDING")
    app.state.vault.put("consent", "old-consent", {"synthetic": True}, 600)
    app.state.vault.put("google_state", "old-state", {"synthetic": True}, 600)
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    backup(settings.database.resolve(), snapshot, key_file)
    principal = asyncio.run(app.state.oauth.load_access_token(old["access_token"]))
    app.state.oauth.disconnect(principal.subject)
    assert asyncio.run(app.state.oauth.load_access_token(old["access_token"])) is None
    recover(snapshot, target, key_file)
    recovered_settings = replace(settings, database=target)
    recovered = create_hosted_app(recovered_settings)
    with TestClient(recovered, base_url=settings.issuer, follow_redirects=False) as http:
        assert call_tool(http, old["access_token"], "list_api_methods", {}).status_code == 401
        assert (
            http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": old_request["client_id"],
                    "refresh_token": old["refresh_token"],
                    "resource": settings.resource,
                },
            ).status_code
            == 401
        )
        assert exchange(http, settings, pending, old_code).status_code == 401
        for namespace, token in [("consent", "old-consent"), ("google_state", "old-state")]:
            assert recovered.state.vault.get(namespace, token, consume=True) is None
        request, fresh = connect_account(
            http, recovered_settings, "com.example.fresh", "SYNTHETIC-FRESH"
        )
    recovered.state.vault.close()
    restarted = create_hosted_app(recovered_settings)
    try:
        with TestClient(restarted, base_url=settings.issuer, follow_redirects=False) as http:
            assert call_tool(http, fresh["access_token"], "list_api_methods", {}).status_code == 200
            response = http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": request["client_id"],
                    "refresh_token": fresh["refresh_token"],
                    "resource": settings.resource,
                },
            )
            assert response.status_code == 200
            assert response.json()["access_token"] != fresh["access_token"]
            assert call_tool(http, old["access_token"], "list_api_methods", {}).status_code == 401
    finally:
        restarted.state.vault.close()


@pytest.mark.parametrize("operation", [backup, recover])
def test_target_created_during_publish_is_preserved(state, monkeypatch, operation):
    from pubship import vault_recovery as recovery

    directory, source, key_file, _, _ = state
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    backup(source, snapshot, key_file)
    link = os.link

    def racing_link(temporary, destination):
        destination.write_bytes(b"concurrent owner data")
        return link(temporary, destination)

    monkeypatch.setattr(recovery.os, "link", racing_link)
    with pytest.raises(PlayError):
        operation(source if operation is backup else snapshot, target, key_file)
    assert target.read_bytes() == b"concurrent owner data"
    assert not list(directory.glob(".vault-recovery-*"))


def test_directory_sync_failure_removes_published_output(state, monkeypatch):
    from pubship import vault_recovery as recovery

    directory, source, key_file, _, _ = state
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    backup(source, snapshot, key_file)
    fsync = os.fsync
    calls = 0

    def sync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic directory sync failure")
        return fsync(fd)

    monkeypatch.setattr(recovery.os, "fsync", sync)
    with pytest.raises(PlayError):
        recover(snapshot, target, key_file)
    assert calls == 2
    assert_no_output(directory, target)


def test_writable_ancestor_rejected(state):
    directory, source, key_file, _, _ = state
    outer = directory / "unsafe"
    outer.mkdir(mode=0o700)
    outer.chmod(0o777)
    inner = outer / "private"
    inner.mkdir(mode=0o700)
    target = inner / "backup.db"
    with pytest.raises(PlayError):
        backup(source, target, key_file)
    assert_no_output(inner, target)


def test_snapshot_sidecars_rejected(state):
    directory, source, key_file, _, _ = state
    snapshot, target = directory / "backup.db", directory / "recovered.db"
    backup(source, snapshot, key_file)
    sidecar = Path(str(snapshot) + "-wal")
    sidecar.write_bytes(b"unaccepted WAL")
    sidecar.chmod(0o600)
    with pytest.raises(PlayError):
        recover(snapshot, target, key_file)
    assert_no_output(directory, target)
