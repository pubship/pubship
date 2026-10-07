"""Operator backup failures use synthetic data and never contact a remote host."""

import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet

from pubship.vault import Vault

SPEC = importlib.util.spec_from_file_location(
    "operator_backup", Path(__file__).parents[1] / "scripts/ops/operator_backup.py"
)
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)
AGE = shutil.which("age")
AGE_KEYGEN = shutil.which("age-keygen")


def write(path, value):
    with ops.new_file(path) as file:
        file.write(value)
    return path


@pytest.fixture
def private(tmp_path):
    path = tmp_path.resolve()
    path.chmod(0o700)
    return path


@pytest.fixture
def age_pair(private, monkeypatch):
    if not AGE or not AGE_KEYGEN:
        pytest.skip(
            "Operator age binaries unavailable; crypto integration is checked on configured hosts"
        )
    identity = private / "age-identity"
    result = subprocess.run([AGE_KEYGEN, "-o", str(identity)], capture_output=True, check=True)
    public = result.stderr.decode().strip().split("Public key: ", 1)[1]
    recipient = write(private / "recipient", public.encode() + b"\n")
    monkeypatch.setattr(ops, "AGE", AGE)
    return identity, recipient


def bundle(*, version="1.2.3", mutation=None):
    values = {"snapshot.age": b"synthetic-age-ciphertext", "vault-key.age": b"synthetic-age-escrow"}
    manifest = {
        "schema": 1,
        "version": version,
        "snapshot_bytes": 4096,
        "encrypted_files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in values.items()
        },
    }
    if mutation == "hash":
        manifest["encrypted_files"]["snapshot.age"]["sha256"] = "0" * 64
    if mutation == "version":
        manifest["version"] = "0.0.0"
    if mutation == "snapshot_size":
        manifest["snapshot_bytes"] = ops.MAX_SNAPSHOT + 1
    items = [("manifest.json", json.dumps(manifest).encode()), *values.items()]
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
        for index, (name, data) in enumerate(items):
            if mutation == "path" and index == 1:
                name = "../snapshot.age"
            if mutation == "duplicate" and index == 1:
                name = "manifest.json"
            info = tarfile.TarInfo(name)
            info.mode, info.size = 0o600, len(data)
            if mutation == "symlink" and index == 1:
                info.type, info.linkname, info.size = tarfile.SYMTYPE, "/etc/passwd", 0
                data = b""
            if mutation == "pax" and index == 1:
                info.type = tarfile.XHDTYPE
            archive.addfile(info, io.BytesIO(data))
    raw = result.getvalue()
    if mutation == "truncated":
        raw = raw[:1000]
    elif mutation == "trailing":
        raw += b"unexpected"
    elif mutation == "oversized":
        first = tarfile.TarInfo("manifest.json")
        first.size = ops.MAX_MANIFEST + 1
        raw = first.tobuf(format=tarfile.USTAR_FORMAT)
    return raw


def received(private, raw):
    scratch = private / "scratch"
    scratch.mkdir(mode=0o700)
    # A real file descriptor exercises the bounded reader without networking.
    with tempfile.TemporaryFile() as stream:
        stream.write(raw)
        stream.seek(0)
        return ops.receive_archive(stream, scratch, "1.2.3", time.monotonic() + 5)


def test_archive_reader_accepts_exact_three_regular_entries(private):
    assert received(private, bundle())["version"] == "1.2.3"
    assert {p.name for p in (private / "scratch").iterdir()} == set(ops.FILES)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (private / "scratch").iterdir())


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "version",
        "snapshot_size",
        "path",
        "duplicate",
        "symlink",
        "pax",
        "truncated",
        "trailing",
        "oversized",
    ],
)
def test_malformed_archive_fails_before_publication(private, mutation):
    with pytest.raises((ops.BackupError, tarfile.TarError)):
        received(private, bundle(mutation=mutation))
    assert not list(private.glob("backup-*"))


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "hardlink", "fifo", "parent"])
def test_file_safety(private, unsafe):
    file = write(private / "file", b"synthetic")
    if unsafe == "symlink":
        alias = private / "alias"
        alias.symlink_to(file)
        file = alias
    elif unsafe == "mode":
        file.chmod(0o644)
    elif unsafe == "hardlink":
        os.link(file, private / "alias")
    elif unsafe == "fifo":
        file.unlink()
        os.mkfifo(file, 0o600)
    else:
        alias = private / "alias"
        alias.symlink_to(private, target_is_directory=True)
        file = alias / "file"
    with pytest.raises(ops.BackupError):
        ops.read_file(file, 100)


def test_configuration_ownership_is_required(private):
    path = write(private / "config", b'{"version":"1.2.3"}')
    with pytest.raises(ops.BackupError):
        ops.load_config(path, owner=os.getuid() + 1)
    assert ops.load_config(path, owner=os.getuid())["version"] == "1.2.3"


def test_source_forced_command_rejects_arbitrary_command(private, monkeypatch):
    monkeypatch.setattr(ops.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "cat /run/secrets/vault-key")
    spawn = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(ops.subprocess, "Popen", spawn)
    with pytest.raises(ops.BackupError, match="command_rejected"):
        ops.source_export({}, io.BytesIO())
    spawn.assert_not_called()


def test_overlap_is_rejected_without_mutating_existing_status(private):
    write(private / "status.json", b'{"status":"stored"}')
    with ops.lock(private):
        with pytest.raises(ops.BackupError, match="already_running"), ops.lock(private):
            pytest.fail("second holder entered")
    assert (private / "status.json").read_bytes() == b'{"status":"stored"}'


def completed(private, number):
    path = private / f"backup-20261006T1200{number:02d}Z-{number:032x}"
    path.mkdir(mode=0o700)
    for name in ops.FILES:
        write(path / name, b"synthetic")
    return path


def test_retention_keeps_seven_and_ignores_unrelated_files(private):
    folders = [completed(private, number) for number in range(10)]
    unrelated = write(private / "unrelated", b"keep")
    assert ops.retain(private, folders[-1]) == 7
    assert [p.exists() for p in folders] == [False] * 3 + [True] * 7
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("unsafe", ["link", "extra", "mode", "hardlink"])
def test_unsafe_retention_candidate_prevents_any_deletion(private, unsafe):
    folders = [completed(private, number) for number in range(8)]
    path = folders[0] / "snapshot.age"
    if unsafe == "link":
        path.unlink()
        path.symlink_to(folders[1] / "snapshot.age")
    elif unsafe == "extra":
        write(folders[0] / "unrelated", b"preserve")
    elif unsafe == "mode":
        path.chmod(0o644)
    else:
        os.link(path, private / "alias")
    with pytest.raises(ops.BackupError):
        ops.retain(private, folders[-1])
    assert all(p.exists() for p in folders)


def test_owned_staging_cleanup_is_bounded(private):
    old = private / ".receiving-abcdefgh"
    old.mkdir(mode=0o700)
    write(old / "snapshot.age", b"partial")
    unrelated = private / ".receiving-not-our-directory"
    unrelated.mkdir(mode=0o700)
    with ops.lock(private):
        ops.cleanup_pending(private, ".receiving-")
    assert not old.exists() and unrelated.exists()


def test_interrupted_retention_resumes_without_blocking_completed_archives(private, monkeypatch):
    folders = [completed(private, number) for number in range(8)]
    unlink = Path.unlink

    def interrupted(path, **kwargs):
        if path.parent.name.startswith(".deleting-") and path.name == "snapshot.age":
            raise OSError("synthetic interrupted deletion")
        return unlink(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(OSError):
            ops.retain(private, folders[-1])
    assert len(ops.completed(private)) == 7
    with ops.lock(private):
        ops.cleanup_pending(private, ".deleting-")
    assert not list(private.glob(".deleting-*"))
    assert all(path.exists() for path in folders[1:])


def test_staging_with_unknown_file_is_not_deleted(private):
    old = private / ".receiving-abcdefgh"
    old.mkdir(mode=0o700)
    write(old / "unrelated", b"preserve")
    with ops.lock(private), pytest.raises(ops.BackupError):
        ops.cleanup_pending(private, ".receiving-")
    assert (old / "unrelated").read_bytes() == b"preserve"


def receiver(private, monkeypatch, *, raw=None, exit_code=0):
    identity = write(private / "ssh-key", b"synthetic-key-not-used")
    known = write(private / "known-hosts", b"synthetic-pinned-host-not-used")
    config = {
        "version": "1.2.3",
        "remote": "root@source.example.invalid",
        "identity_file": str(identity),
        "known_hosts_file": str(known),
        "state_directory": str(private),
    }
    regular = ops.regular
    monkeypatch.setattr(
        ops,
        "regular",
        lambda p, **kw: regular(
            p, **{**kw, **({"owner": os.getuid()} if kw.get("owner") == 0 else {})}
        ),
    )
    monkeypatch.setattr(ops.os, "geteuid", lambda: 12345)
    artifact = write(private / "input.tar", bundle() if raw is None else raw)
    popen = subprocess.Popen

    def fake_ssh(command, **kwargs):
        assert command[:4] == [ops.SSH, "-F", "/dev/null", "-T"]
        assert command[-1] == "export"
        assert "StrictHostKeyChecking=yes" in command and "IdentitiesOnly=yes" in command
        assert "ProxyCommand=none" in command and "PermitLocalCommand=no" in command
        assert "GlobalKnownHostsFile=/dev/null" in command and "IdentityAgent=none" in command
        return popen(
            [
                sys.executable,
                "-c",
                "import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes());sys.stdout.flush();raise SystemExit(int(sys.argv[2]))",
                str(artifact),
                str(exit_code),
            ],
            **kwargs,
        )

    monkeypatch.setattr(ops.subprocess, "Popen", fake_ssh)
    return config


def test_manager_publishes_complete_ciphertext_and_reports_status(private, monkeypatch):
    config = receiver(private, monkeypatch)
    receipt = ops.pull(config)
    assert receipt["status"] == "stored" and receipt["retained"] == 1
    assert receipt["decryption_checked"] is False and receipt["live_vault_restored"] is False
    assert {p.name for p in (private / receipt["archive"]).iterdir()} == set(ops.FILES)
    assert not list(private.glob(".receiving-*"))
    assert ops.status(config)["healthy"] is True
    assert (
        ops.status(config, now=receipt["last_success"] + ops.STALE_SECONDS + 1)["healthy"] is False
    )


@pytest.mark.parametrize("failure", ["truncated", "exit", "sync"])
def test_manager_failure_never_publishes_partial_and_preserves_last_success(
    private, monkeypatch, failure
):
    config = receiver(
        private,
        monkeypatch,
        raw=bundle(mutation="truncated") if failure == "truncated" else None,
        exit_code=1 if failure == "exit" else 0,
    )
    ops.save_json(private / "status.json", {"status": "stored", "last_success": 1234})
    if failure == "sync":
        original = ops.sync_directory

        def failing(path):
            if path.name.startswith(".receiving-"):
                raise OSError("synthetic details must not escape")
            return original(path)

        monkeypatch.setattr(ops, "sync_directory", failing)
    with pytest.raises(ops.BackupError):
        ops.pull(config)
    assert not list(private.glob("backup-*"))
    assert not list(private.glob(".receiving-*"))
    receipt = json.loads((private / "status.json").read_bytes())
    assert receipt["last_success"] == 1234 and receipt["status"] == "failed"
    assert "synthetic details" not in json.dumps(receipt)
    assert ops.status(config)["healthy"] is False


def test_deadline_releases_blocked_reader(private):
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "rb") as stream:
            with pytest.raises(ops.BackupError, match="deadline"):
                ops._exact(stream, 1, time.monotonic() + 0.01)
    finally:
        os.close(write_fd)


def test_main_error_redacts_arbitrary_exception(monkeypatch, capsys):
    monkeypatch.setattr(ops, "load_config", Mock(side_effect=RuntimeError("synthetic-secret")))
    assert ops.main(["pull"]) == 1
    message = capsys.readouterr()
    assert message.out == "" and "synthetic-secret" not in message.err
    assert json.loads(message.err)["notification_delivered"] is False


@pytest.mark.parametrize("failure", [None, "version", "age", "orphan"])
def test_source_real_age_export_operator_decrypt_and_empty_recovery(
    private, age_pair, monkeypatch, failure
):
    from pubship.vault_recovery import recover

    identity, recipient = age_pair
    runtime = private / "runtime"
    runtime.mkdir(mode=0o700)
    key = Fernet.generate_key()
    write(runtime / "key", key)
    vault = Vault(runtime / "vault.db", key)
    vault.put("grants", "synthetic-family", {"client_id": "synthetic"}, 600)
    vault.close()
    source_before = (runtime / "vault.db").read_bytes()
    staging = private / "source"
    staging.mkdir(mode=0o700)
    version = importlib.metadata.version("pubship")
    config = {
        "version": version,
        "container": "synthetic-container",
        "state_directory": str(staging),
        "recipient_file": str(recipient),
    }
    monkeypatch.setattr(ops.os, "geteuid", lambda: 0)
    # Safety gates must survive optimized Python inherited by docker exec.
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    original_read = ops.read_file
    monkeypatch.setattr(
        ops,
        "read_file",
        lambda p, maximum, **kw: original_read(
            p, maximum, **{**kw, **({"owner": os.getuid()} if kw.get("owner") == 0 else {})}
        ),
    )
    popen = subprocess.Popen

    def local_container(command, **kwargs):
        if command[0] == ops.DOCKER:
            code = (
                command[-1]
                .replace("/var/lib/pubship/vault.db", str(runtime / "vault.db"))
                .replace("/var/lib/pubship/operator-backup-", str(runtime / "operator-backup-"))
                .replace("/run/secrets/vault-key", str(runtime / "key"))
                .replace("10001", str(os.getuid()))
            )
            return popen([sys.executable, "-c", code], **kwargs)
        assert command[0] == ops.AGE
        return popen(command, **kwargs)

    monkeypatch.setattr(ops.subprocess, "Popen", local_container)
    ciphertext = private / "export.tar"
    if failure:
        if failure == "version":
            config["version"] = "0.0.0"
        elif failure == "age":
            monkeypatch.setattr(ops, "AGE", "/usr/bin/false")
        else:
            write(runtime / "operator-backup-orphan.db", b"private interrupted snapshot")
        with pytest.raises(ops.BackupError), ops.new_file(ciphertext) as output:
            ops.source_export(config, output)
        assert ciphertext.stat().st_size == 0
        assert not list(staging.glob(".export-*"))
        assert (runtime / "vault.db").read_bytes() == source_before
        if failure == "orphan":
            assert (
                runtime / "operator-backup-orphan.db"
            ).read_bytes() == b"private interrupted snapshot"
        return
    with ops.new_file(ciphertext) as output:
        ops.source_export(config, output)
    assert key not in ciphertext.read_bytes()
    destination = private / "received"
    destination.mkdir(mode=0o700)
    with ciphertext.open("rb") as stream:
        ops.receive_archive(stream, destination, version, time.monotonic() + 5)
    # Only the operator-side test process has the age identity. No shell command
    # or source/receiver configuration includes the identity contents.
    decrypted = private / "restore"
    decrypted.mkdir(mode=0o700)
    for encrypted, output in [("snapshot.age", "snapshot.db"), ("vault-key.age", "key")]:
        with ops.new_file(decrypted / output) as file:
            result = subprocess.run(
                [AGE, "--decrypt", "--identity", str(identity), str(destination / encrypted)],
                stdout=file,
                stderr=subprocess.DEVNULL,
            )
        assert result.returncode == 0
    assert (decrypted / "key").read_bytes() == key
    recover(decrypted / "snapshot.db", decrypted / "empty.db", decrypted / "key")
    for _ in range(2):
        restored = Vault(decrypted / "empty.db", key)
        assert restored.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
        restored.close()
    assert (runtime / "vault.db").read_bytes() == source_before
    assert not list(runtime.glob("operator-backup-*"))
    assert not list(staging.glob(".export-*"))


def test_age_failure_prevents_publication(private, age_pair, monkeypatch):
    _, recipient = age_pair
    output = private / "encrypted.age"
    monkeypatch.setattr(ops, "AGE", "/usr/bin/false")
    with pytest.raises(ops.BackupError, match="subprocess"), ops.new_file(private / "unused"):
        ops.encrypt_stream(
            io.BytesIO(b"synthetic").read, 9, output, recipient, time.monotonic() + 5
        )
    # Caller owns its private staging directory and removes failed ciphertext;
    # no archive can be published from this unsuccessful encryption call.
    assert not list(private.glob("backup-*"))


@pytest.mark.parametrize("damage", ["missing", "size", "version", "future", "nan"])
def test_health_rejects_missing_archive_and_invalid_receipt(private, monkeypatch, damage):
    config = receiver(private, monkeypatch)
    receipt = ops.pull(config)
    if damage == "missing":
        (private / receipt["archive"] / "vault-key.age").unlink()
    elif damage == "size":
        (private / receipt["archive"] / "snapshot.age").write_bytes(b"truncated")
    elif damage == "version":
        config["version"] = "0.0.0"
    else:
        receipt["last_success"] = time.time() + 100 if damage == "future" else float("nan")
        ops.save_json(private / "status.json", receipt)
    assert ops.status(config)["healthy"] is False


def test_blocked_export_writer_obeys_deadline():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(write_fd, "wb") as stream:
            with pytest.raises(ops.BackupError, match="deadline"):
                ops.Writer(stream, time.monotonic() + 0.01).write(b"x" * 1024**2)
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("damage", ["wrong_identity", "corrupt"])
def test_real_age_authentication_failure_cannot_reach_recovery(private, age_pair, damage):
    identity, recipient = age_pair
    encrypted = private / "snapshot.age"
    data = b"synthetic snapshot"
    ops.encrypt_stream(io.BytesIO(data).read, len(data), encrypted, recipient, time.monotonic() + 5)
    if damage == "wrong_identity":
        identity = private / "different-identity"
        subprocess.run([AGE_KEYGEN, "-o", str(identity)], capture_output=True, check=True)
    else:
        raw = bytearray(encrypted.read_bytes())
        raw[-1] ^= 1
        encrypted.write_bytes(raw)
    with ops.new_file(private / "decrypted") as output:
        result = subprocess.run(
            [AGE, "--decrypt", "--identity", str(identity), str(encrypted)],
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
    assert result.returncode != 0
    assert not (private / "recovered.db").exists()


def test_closed_export_pipe_reports_subprocess_failure():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    with os.fdopen(write_fd, "wb", buffering=0) as stream:
        with pytest.raises(ops.BackupError, match="^subprocess_failed$"):
            ops.Writer(stream, time.monotonic() + 1).write(b"synthetic backup bytes")
