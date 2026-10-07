"""Offline, bounded backup and reconnect-only recovery of a private hosted vault.

Run with ``python -m pubship.vault_recovery --help``. Recovery never
copies a credential into its output: an old backup cannot prove later revocations.
"""

import argparse
import json
import math
import os
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .errors import PlayError
from .vault import Vault, private_file

MAX_BYTES = 512 * 1024**2
MAX_RECORDS = Vault.MAX_CONNECTIONS * Vault.CONNECTION_LIMIT + sum(Vault.ADMISSION_LIMITS.values())
MAX_SECONDS = 30
MARKER = b"pubship-vault-backup-v1:reconnect-required"
FAILURE = (
    "Vault operation failed; check private paths, matching key, snapshot integrity and limits."
)


def _path(path):
    path = Path(os.path.abspath(path))
    # Reject symlinks in every component, including ancestors of the private
    # directory. Operators on macOS should use /private/... instead of /var/....
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise PlayError(FAILURE)
        if component != path:
            info = component.stat()
            if info.st_uid not in {0, os.getuid()} or (
                info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
            ):
                raise PlayError(FAILURE)
    parent = path.parent.stat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_IMODE(parent.st_mode) != 0o700
        or parent.st_uid != os.getuid()
    ):
        raise PlayError(FAILURE)
    return path


def _file(path, *, maximum=None):
    maximum = MAX_BYTES if maximum is None else maximum
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise PlayError(FAILURE)
    finally:
        os.close(fd)


def _sidecars(path, *, snapshot=False):
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink() or (snapshot and sidecar.exists()):
            raise PlayError(FAILURE)
        if sidecar.exists():
            _file(sidecar)


def _deadline():
    end = time.monotonic() + MAX_SECONDS

    def check():
        if time.monotonic() >= end:
            raise PlayError(FAILURE)

    return check


def _configure(db, check):
    db.execute("PRAGMA trusted_schema=OFF")
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 128 * 1024)

    def progress():
        check()
        return 0

    db.set_progress_handler(progress, 1000)


def _validate(db, cipher, check, *, marker):
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise PlayError(FAILURE)
    if marker:
        rows = db.execute("SELECT payload FROM vault_backup_metadata LIMIT 2").fetchall()
        if len(rows) != 1 or cipher.decrypt(rows[0][0]) != MARKER:
            raise PlayError(FAILURE)
    count = 0
    for namespace, digest, expires, payload in db.execute(
        "SELECT namespace,key,expires,payload FROM records"
    ):
        check()
        count += 1
        if count > MAX_RECORDS or not isinstance(payload, bytes) or len(payload) > 90_000:
            raise PlayError(FAILURE)
        decoded = cipher.decrypt(payload)
        if len(decoded) > 64 * 1024:
            raise PlayError(FAILURE)
        record = json.loads(decoded)
        # Authenticate expired records too: recovery must not silently drop an
        # unreadable record and mistake a wrong key for successful verification.
        if (
            not isinstance(record, dict)
            or not isinstance(namespace, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or record.get("namespace") != namespace
            or record.get("key") != digest
            or record.get("expires") != expires
            or "value" not in record
        ):
            raise PlayError(FAILURE)
    check()


@contextmanager
def _output(target):
    """Publish only a closed, complete file; hard-link publication cannot replace."""
    fd, name = tempfile.mkstemp(prefix=".vault-recovery-", dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    published = False
    try:
        yield temporary
        _file(temporary)
        _sidecars(temporary, snapshot=True)
        with temporary.open("rb") as completed:
            os.fsync(completed.fileno())
        os.link(temporary, target)
        published = True
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if published:
            target.unlink()
        raise
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def _inputs(source, target, key_file, *, snapshot):
    source, target, key_file = (_path(p) for p in (source, target, key_file))
    if source == target or target.exists():
        raise PlayError(FAILURE)
    _sidecars(target, snapshot=True)
    _file(source)
    _sidecars(source, snapshot=snapshot)
    _file(key_file, maximum=64 * 1024)
    key = private_file(key_file).strip()
    return source, target, key, Fernet(key)


def backup(source: Path, target: Path, key_file: Path):
    """Create a new private SQLite online snapshot, including committed WAL state."""
    try:
        source, target, _, cipher = _inputs(source, target, key_file, snapshot=False)
        check = _deadline()
        with _output(target) as temporary:
            with (
                closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as src,
                closing(sqlite3.connect(temporary, timeout=1)) as dst,
            ):
                _configure(src, check)
                _configure(dst, check)
                page_size = src.execute("PRAGMA page_size").fetchone()[0]

                def progress(status, remaining, total):
                    check()
                    if total * page_size > MAX_BYTES:
                        raise PlayError(FAILURE)

                src.backup(dst, pages=64, progress=progress, sleep=0.05)
                dst.execute("PRAGMA journal_mode=DELETE")
                _validate(dst, cipher, check, marker=False)
                dst.execute("DROP TABLE IF EXISTS vault_backup_metadata")
                dst.execute("CREATE TABLE vault_backup_metadata (payload BLOB NOT NULL)")
                dst.execute(
                    "INSERT INTO vault_backup_metadata VALUES (?)", (cipher.encrypt(MARKER),)
                )
                dst.commit()
                check()
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError, InvalidToken, PlayError):
        raise PlayError(FAILURE) from None


def recover(source: Path, target: Path, key_file: Path):
    """Validate the backup/key, then create an EMPTY vault requiring reconnection."""
    try:
        source, target, key, cipher = _inputs(source, target, key_file, snapshot=True)
        check = _deadline()
        # Only self-contained backup files are accepted, so immutable mode cannot
        # hide committed WAL data and ensures validation creates no source files.
        with closing(
            sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        ) as db:
            _configure(db, check)
            _validate(db, cipher, check, marker=True)
        with _output(target) as temporary:
            vault = Vault(temporary, key)
            vault.close()
            check()
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError, InvalidToken, PlayError):
        raise PlayError(FAILURE) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "recover"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        {"backup": backup, "recover": recover}[args.operation](
            args.source, args.target, args.key_file
        )
    except PlayError:
        print(FAILURE, file=sys.stderr)
        return 1
    print(
        "Private backup created."
        if args.operation == "backup"
        else "Empty vault created; reconnect all clients."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
