#!/usr/bin/env python3
"""Bounded age-encrypted off-server backup, with no decryption identity on servers.

Linux operator deployment only; see docs/vault-recovery.md. The export SSH key may
request only `export`. Configuration is root-owned; commands are fixed binaries.
"""

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

AGE = "/usr/bin/age"
SSH = "/usr/bin/ssh"
DOCKER = "/usr/bin/docker"
SOURCE_CONFIG = Path("/etc/pubship-backup/source.json")
RECEIVER_CONFIG = Path("/etc/pubship-backup/receiver.json")
MAX_SNAPSHOT = 512 * 1024**2
MAX_CIPHERTEXT = MAX_SNAPSHOT + 1024**2
MAX_ESCROW = 4096
MAX_MANIFEST = 4096
MAX_STREAM = MAX_CIPHERTEXT + MAX_ESCROW + MAX_MANIFEST + 32 * 1024
SECONDS = 240
STALE_SECONDS = 36 * 3600
KEEP = 7
MIN_FREE = 2 * 1024**3
COMPLETE = re.compile(r"backup-\d{8}T\d{6}Z-[a-f0-9]{32}\Z")
FILES = ("manifest.json", "snapshot.age", "vault-key.age")
ACTION = "Inspect backup service, pinned SSH/recipient configuration, source version and free disk; rerun before deployment."

# Runs only inside the accepted container as its non-root vault owner. It never
# writes a plaintext key file. Its stdout goes to the root-owned source exporter,
# which encrypts on the SOURCE before any SSH response reaches the receiver.
INNER = r"""
import importlib.metadata,json,logging,os,stat,sys
from pathlib import Path
from pubship.vault import private_file
from pubship.vault_recovery import backup
logging.disable(logging.CRITICAL)
def check(value):
    if not value: raise SystemExit(1)
p = Path('/var/lib/pubship/operator-backup-NONCE.db')
created = None
try:
    check(os.getuid() == 10001)
    check(importlib.metadata.version('pubship') == 'VERSION')
    check(not p.exists() and not p.is_symlink())
    # A killed process can leave a private snapshot or recovery staging file.
    # Fail closed for operator inspection instead of accumulating or deleting it.
    check(not any(p.parent.glob('operator-backup-*')))
    check(not any(p.parent.glob('.vault-recovery-*')))
    key_path = Path('/run/secrets/vault-key')
    key = private_file(key_path).strip()
    check(len(key) == 44)
    space = os.statvfs(p.parent)
    check(space.f_bavail * space.f_frsize >= 2 * 1024**3)
    backup(Path('/var/lib/pubship/vault.db'), p, key_path)
    created = p.lstat()
    check(stat.S_ISREG(created.st_mode) and created.st_uid == 10001)
    check(stat.S_IMODE(created.st_mode) == 0o600 and created.st_nlink == 1)
    check(0 < created.st_size <= 512 * 1024**2)
    check(private_file(key_path).strip() == key)
    out = sys.stdout.buffer
    out.write(json.dumps({'version':'VERSION','size':created.st_size}).encode()+b'\n')
    out.write(key)
    with p.open('rb') as file:
        while chunk := file.read(65536): out.write(chunk)
    out.flush()
except BaseException:
    raise SystemExit(1)
finally:
    if created is not None:
        current = p.lstat()
        if (created.st_dev,created.st_ino) != (current.st_dev,current.st_ino):
            raise SystemExit(1)
        p.unlink()
"""


class BackupError(Exception):
    pass


def require(value, category="invalid_backup"):
    if not value:
        raise BackupError(category)


def safe_path(path):
    path = Path(path)
    require(path.is_absolute(), "unsafe_path")
    for part in [*reversed(path.parents), path]:
        require(not part.is_symlink(), "unsafe_path")
        if part != path and part.exists():
            info = part.stat()
            require(info.st_uid in {0, os.getuid()}, "unsafe_path")
            require(not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX), "unsafe_path")
    return path


def private_directory(path):
    path = safe_path(path)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), "unsafe_directory")
    require(stat.S_IMODE(info.st_mode) == 0o700, "unsafe_directory")
    return path


def regular(path, *, maximum, owner=None, public=False):
    path = safe_path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_file")
    require(info.st_uid == (os.getuid() if owner is None else owner), "unsafe_file")
    modes = {0o400, 0o600, 0o644, 0o444} if public else {0o600}
    require(stat.S_IMODE(info.st_mode) in modes and info.st_size <= maximum, "unsafe_file")
    return info


def read_file(path, maximum, **kwargs):
    regular(path, maximum=maximum, **kwargs)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        data = file.read(maximum + 1)
    require(len(data) <= maximum, "oversized_file")
    return data


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def new_file(path):
    safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def save_json(path, value):
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        regular(path, maximum=MAX_MANIFEST)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(json.dumps(value, sort_keys=True).encode())
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def load_config(path, *, owner=0):
    value = json.loads(read_file(path, MAX_MANIFEST, owner=owner, public=True))
    require(isinstance(value, dict), "invalid_configuration")
    require(
        re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", value.get("version", "")),
        "invalid_configuration",
    )
    return value


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    require(remaining > 0, "deadline_exceeded")
    return remaining


def _read(stream, size, deadline):
    ready, _, _ = select.select([stream], [], [], _remaining(deadline))
    require(ready, "deadline_exceeded")
    return os.read(stream.fileno(), size)


def _exact(stream, size, deadline):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = _read(stream, min(65536, size - len(chunks)), deadline)
        require(chunk, "truncated_stream")
        chunks.extend(chunk)
    return bytes(chunks)


def _write(stream, data, deadline):
    os.set_blocking(stream.fileno(), False)
    view = memoryview(data)
    while view:
        _, ready, _ = select.select([], [stream], [], _remaining(deadline))
        require(ready, "deadline_exceeded")
        try:
            count = os.write(stream.fileno(), view)
        except BlockingIOError:
            continue
        except BrokenPipeError:
            raise BackupError("subprocess_failed") from None
        view = view[count:]


def _wait(process, deadline):
    require(process.wait(timeout=_remaining(deadline)) == 0, "subprocess_failed")


def _stop(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            stream.close()


def encrypt_stream(source, length, destination, recipient, deadline):
    """age performs standard authenticated encryption; Python only copies bytes."""
    with new_file(destination) as output:
        process = subprocess.Popen(
            [AGE, "--encrypt", "--recipients-file", str(recipient)],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
        try:
            remaining = length
            while remaining:
                chunk = source(min(65536, remaining))
                require(chunk and len(chunk) <= remaining, "truncated_stream")
                _write(process.stdin, chunk, deadline)
                remaining -= len(chunk)
            process.stdin.close()
            _wait(process, deadline)
            output.flush()
            os.fsync(output.fileno())
        finally:
            _stop(process)
    regular(destination, maximum=MAX_CIPHERTEXT if length > 44 else MAX_ESCROW)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(65536):
            result.update(chunk)
    return result.hexdigest()


@contextlib.contextmanager
def lock(root):
    private_directory(root)
    path = root / "operation.lock"
    if path.exists() or path.is_symlink():
        regular(path, maximum=0)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("backup_already_running") from None
        yield
    finally:
        os.close(fd)


def source_export(config, output):
    require(os.geteuid() == 0, "source_requires_root")
    require(os.environ.get("SSH_ORIGINAL_COMMAND", "") in {"", "export"}, "command_rejected")
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", config.get("container", "")),
        "invalid_configuration",
    )
    root = private_directory(Path(config["state_directory"]))
    recipient = Path(config["recipient_file"])
    public = read_file(recipient, 1024, owner=0, public=True).strip()
    require(re.fullmatch(rb"age1[a-z0-9]{58}", public), "invalid_recipient")
    deadline = time.monotonic() + SECONDS
    with lock(root):
        cleanup_pending(root, ".export-")
        space = os.statvfs(root)
        require(space.f_bavail * space.f_frsize >= MIN_FREE, "insufficient_disk")
        return _source_export(config, output, recipient, root, deadline)


class Writer:
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline

    def write(self, value):
        _write(self.stream, value, self.deadline)
        return len(value)


def _source_export(config, output, recipient, root, deadline):
    with tempfile.TemporaryDirectory(prefix=".export-", dir=root) as temporary:
        scratch = Path(temporary)
        code = INNER.replace("NONCE", uuid.uuid4().hex).replace("VERSION", config["version"])
        process = subprocess.Popen(
            [DOCKER, "exec", "--user", "10001", config["container"], "python", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            header = bytearray()
            while not header.endswith(b"\n"):
                require(len(header) < 1024, "invalid_source_header")
                header.extend(_exact(process.stdout, 1, deadline))
            metadata = json.loads(header)
            require(metadata.get("version") == config["version"], "version_mismatch")
            size = metadata.get("size")
            require(type(size) is int and 0 < size <= MAX_SNAPSHOT, "oversized_snapshot")
            key = io.BytesIO(_exact(process.stdout, 44, deadline))
            encrypt_stream(key.read, 44, scratch / "vault-key.age", recipient, deadline)
            encrypt_stream(
                lambda n: _exact(process.stdout, n, deadline),
                size,
                scratch / "snapshot.age",
                recipient,
                deadline,
            )
            require(not _read(process.stdout, 1, deadline), "trailing_source_data")
            _wait(process, deadline)  # Includes successful removal of the raw source snapshot.
        finally:
            _stop(process)
        manifest = {
            "schema": 1,
            "version": config["version"],
            "snapshot_bytes": size,
            "encrypted_files": {
                name: {"bytes": (scratch / name).stat().st_size, "sha256": digest(scratch / name)}
                for name in FILES[1:]
            },
        }
        data = json.dumps(manifest, sort_keys=True).encode()
        with tarfile.open(
            fileobj=Writer(output, deadline), mode="w|", format=tarfile.USTAR_FORMAT
        ) as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
            for name in FILES[1:]:
                with (scratch / name).open("rb") as file:
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = (scratch / name).stat().st_size, 0o600
                    archive.addfile(info, file)


class Reader:
    def __init__(self, stream, deadline):
        self.stream, self.deadline, self.total = stream, deadline, 0

    def exact(self, size):
        require(size <= 65536, "oversized_chunk")
        self.total += size
        require(self.total <= MAX_STREAM, "oversized_stream")
        return _exact(self.stream, size, self.deadline)


def receive_archive(stream, scratch, version, deadline):
    """Accept only three ordinary USTAR files; never use tar extraction paths.

    Parsing each fixed-size header before its payload rejects oversized/PAX/link
    entries before a library might allocate their attacker-controlled metadata.
    """
    reader = Reader(stream, deadline)
    hashes, sizes = {}, {}
    for expected in FILES:
        info = tarfile.TarInfo.frombuf(reader.exact(512), "utf-8", "strict")
        limit = (
            MAX_MANIFEST
            if expected == "manifest.json"
            else MAX_ESCROW
            if expected == "vault-key.age"
            else MAX_CIPHERTEXT
        )
        require(
            info.name == expected and info.type in {tarfile.REGTYPE, tarfile.AREGTYPE},
            "invalid_archive_entry",
        )
        require(not info.linkname and 0 < info.size <= limit, "oversized_archive_entry")
        remaining, checksum = info.size, hashlib.sha256()
        with new_file(scratch / expected) as file:
            while remaining:
                chunk = reader.exact(min(65536, remaining))
                file.write(chunk)
                checksum.update(chunk)
                remaining -= len(chunk)
            file.flush()
            os.fsync(file.fileno())
        padding = (-info.size) % 512
        require(not any(reader.exact(padding)), "invalid_archive_padding")
        hashes[expected], sizes[expected] = checksum.hexdigest(), info.size
    require(not any(reader.exact(1024)), "invalid_archive_trailer")
    # USTAR writers pad their last record; accept bounded zero padding only.
    padding = 0
    while chunk := _read(stream, 65536, deadline):
        padding += len(chunk)
        require(padding <= 10240 and not any(chunk), "trailing_archive_data")
    manifest = json.loads(read_file(scratch / "manifest.json", MAX_MANIFEST))
    require(manifest.get("schema") == 1 and manifest.get("version") == version, "version_mismatch")
    require(
        type(manifest.get("snapshot_bytes")) is int
        and 0 < manifest["snapshot_bytes"] <= MAX_SNAPSHOT,
        "oversized_snapshot",
    )
    require(set(manifest.get("encrypted_files", {})) == set(FILES[1:]), "invalid_manifest")
    for name in FILES[1:]:
        require(
            manifest["encrypted_files"][name] == {"bytes": sizes[name], "sha256": hashes[name]},
            "ciphertext_mismatch",
        )
    return manifest


def completed(root):
    result = []
    for path in root.iterdir():
        if not COMPLETE.fullmatch(path.name):
            continue
        private_directory(path)
        require({entry.name for entry in path.iterdir()} == set(FILES), "unsafe_retention_entry")
        for name in FILES:
            regular(path / name, maximum=MAX_CIPHERTEXT if name == "snapshot.age" else MAX_MANIFEST)
        result.append(path)
    return sorted(result, key=lambda p: p.name, reverse=True)


def cleanup_pending(root, prefix):
    # Called only under the job lock. Delete only this tool's private staging
    # directories and its exact regular filenames, including interrupted writes.
    pattern = re.compile(re.escape(prefix) + r"[a-z0-9_]{8}\Z")
    candidates = []
    for path in root.iterdir():
        if not pattern.fullmatch(path.name):
            continue
        private_directory(path)
        entries = list(path.iterdir())
        require({entry.name for entry in entries} <= set(FILES), "unsafe_staging_entry")
        for entry in entries:
            regular(entry, maximum=MAX_CIPHERTEXT if entry.name == "snapshot.age" else MAX_MANIFEST)
        candidates.append((path, entries))
    for path, entries in candidates:
        for entry in entries:
            entry.unlink()
        path.rmdir()


def retain(root, newest):
    candidates = completed(root)
    require(newest in candidates, "missing_new_archive")
    keep = {newest, *[path for path in candidates if path != newest][: KEEP - 1]}
    for path in candidates:
        if path not in keep:
            pending = root / (".deleting-" + uuid.uuid4().hex[:8])
            require(not pending.exists() and not pending.is_symlink(), "target_exists")
            os.rename(path, pending)
            sync_directory(root)
            for name in FILES:
                (pending / name).unlink()
            pending.rmdir()
    sync_directory(root)
    return len(keep)


def previous(root):
    path = root / "status.json"
    return json.loads(read_file(path, MAX_MANIFEST)) if path.exists() else {}


def pull(config):
    require(os.geteuid() != 0, "receiver_requires_unprivileged_user")
    root = private_directory(Path(config["state_directory"]))
    require(
        re.fullmatch(
            r"[a-z_][a-z0-9_-]{0,31}@[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", config.get("remote", "")
        ),
        "invalid_configuration",
    )
    identity, known_hosts = Path(config["identity_file"]), Path(config["known_hosts_file"])
    regular(identity, maximum=16384)  # SSH reads it; Python never reads the private SSH key.
    regular(known_hosts, maximum=65536, owner=0, public=True)
    with lock(root):
        before = previous(root)
        started = time.time()
        try:
            cleanup_pending(root, ".receiving-")
            cleanup_pending(root, ".deleting-")
            completed(root)  # Validate all retention candidates before network work.
            space = os.statvfs(root)
            require(space.f_bavail * space.f_frsize >= MIN_FREE, "insufficient_disk")
            deadline = time.monotonic() + SECONDS
            command = [
                SSH,
                "-F",
                "/dev/null",
                "-T",
                "-i",
                str(identity),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "ProxyCommand=none",
                "-o",
                "ProxyJump=none",
                "-o",
                "UserKnownHostsFile=" + str(known_hosts),
                "-o",
                "GlobalKnownHostsFile=/dev/null",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "IdentityAgent=none",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=2",
                config["remote"],
                "export",
            ]
            with tempfile.TemporaryDirectory(prefix=".receiving-", dir=root) as name:
                scratch = Path(name)
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    manifest = receive_archive(process.stdout, scratch, config["version"], deadline)
                    _wait(process, deadline)
                finally:
                    _stop(process)
                sync_directory(scratch)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                target = root / ("backup-" + stamp + "-" + uuid.uuid4().hex)
                require(not target.exists() and not target.is_symlink(), "target_exists")
                # Both directories are private under the single held job lock.
                os.rename(scratch, target)
                sync_directory(root)
            retained = retain(root, target)
            receipt = {
                "schema": 1,
                "status": "stored",
                "last_attempt": started,
                "last_success": time.time(),
                "version": config["version"],
                "snapshot_bytes": manifest["snapshot_bytes"],
                "archive": target.name,
                "retained": retained,
                "decryption_checked": False,
                "live_vault_restored": False,
            }
            save_json(root / "status.json", receipt)
            return receipt
        except Exception as error:
            category = str(error) if isinstance(error, BackupError) else "backup_failed"
            receipt = {
                "schema": 1,
                "status": "failed",
                "last_attempt": started,
                "last_success": before.get("last_success"),
                "category": category,
                "action": ACTION,
            }
            save_json(root / "status.json", receipt)
            raise BackupError(category) from None


def status(config, *, now=None):
    root = private_directory(Path(config["state_directory"]))
    receipt = previous(root)
    now = time.time() if now is None else now
    success = receipt.get("last_success")
    age = now - success if type(success) in {int, float} and math.isfinite(success) else None
    stored = receipt.get("status") == "stored"
    archive_present = False
    if stored:
        name = receipt.get("archive", "")
        if isinstance(name, str) and COMPLETE.fullmatch(name):
            try:
                path = root / name
                archive_present = path in completed(root)
                manifest = json.loads(read_file(path / "manifest.json", MAX_MANIFEST))
                archive_present = archive_present and manifest.get("version") == config["version"]
                for encrypted in FILES[1:]:
                    archive_present = archive_present and (
                        manifest["encrypted_files"][encrypted]["bytes"]
                        == (path / encrypted).stat().st_size
                    )
            except (OSError, ValueError, KeyError, TypeError, BackupError):
                archive_present = False
    healthy = age is not None and 0 <= age <= STALE_SECONDS and stored and archive_present
    return {
        "schema": 1,
        "healthy": healthy,
        "backup_age_seconds": int(age) if age is not None else None,
        "last_result": receipt.get("status", "missing"),
        "archive_present": archive_present,
        "action": None if healthy else ACTION,
        "notification_delivered": False,
    }


def interrupted(*_):
    raise BackupError("interrupted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("export", "pull", "status", "report"))
    args = parser.parse_args(argv)
    for name in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(name, interrupted)
    try:
        config = load_config(SOURCE_CONFIG if args.operation == "export" else RECEIVER_CONFIG)
        if args.operation == "export":
            require(not sys.stdout.isatty(), "terminal_export_rejected")
            source_export(config, sys.stdout.buffer)
            return 0
        receipt = pull(config) if args.operation == "pull" else status(config)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if args.operation in {"pull", "report"} or receipt["healthy"] else 1
    except Exception as error:
        category = str(error) if isinstance(error, BackupError) else "backup_failed"
        print(
            json.dumps(
                {
                    "status": "failed",
                    "category": category,
                    "action": ACTION,
                    "notification_delivered": False,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
