"""Pinned local directories, private immutable spools and atomic artifact outputs."""

import hashlib
import math
import os
import secrets
import stat
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol

from .errors import PlayError

CHUNK_BYTES = 1024 * 1024
FILE_ERROR = "Artifact file access failed; use an authorized regular file in the configured root."


class SnapshotLease(Protocol):
    """Trusted immutable transfer source; disposal status owns quota release."""

    stream: object
    size: int
    sha1: str
    sha256: str
    deadline: float

    def metadata(self) -> dict: ...
    def dispose(self) -> str: ...
    def close(self) -> str: ...


class DestinationLease(Protocol):
    """Private bounded sink whose publication exposes a handle, never a path."""

    cap: int
    size: int
    deadline: float

    def __enter__(self): ...
    def __exit__(self, *args): ...
    def write(self, chunk: bytes) -> None: ...
    def publish(self) -> dict: ...
    def dispose(self) -> str: ...


def media_deadline(deadline, *resources):
    """Clamp to original authorization/lease deadlines, never a refreshed lifetime."""
    for resource in resources:
        value = getattr(resource, "deadline", None)
        if value is not None:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise PlayError("Invalid media authorization deadline.")
            deadline = min(deadline, value)
    return deadline


def dispose_snapshot(snapshot):
    if snapshot is None:
        return "disposed"
    try:
        dispose = getattr(snapshot, "dispose", None)
        if dispose is not None:
            result = dispose()
            return (
                result
                if result in {"disposed", "deferred-busy", "cleanup-pending"}
                else "cleanup-pending"
            )
        result = snapshot.close()
        if result in {"disposed", "deferred-busy", "cleanup-pending"}:
            return result
        return "disposed" if snapshot.stream.closed else "cleanup-pending"
    except Exception:
        # Preserve the reference/reservation and the original provider outcome.
        return "cleanup-pending"


def retained_metadata(preparation):
    """Internal accounting metadata; credentials and live file objects never escape."""
    result = {
        name: deepcopy(value)
        for name, value in vars(preparation).items()
        if name not in {"token", "snapshot"}
    }
    result["credential_bytes"] = len(preparation.token.encode("utf-8"))
    result["snapshot"] = (
        preparation.snapshot.metadata() if preparation.snapshot is not None else None
    )
    return result


def require_snapshot(snapshot, cap):
    if type(snapshot.size) is not int or not 0 < snapshot.size <= cap:
        raise PlayError("Prepared media exceeds its effective byte limit.")


def basename(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or any(not c.isprintable() for c in value)
    ):
        raise PlayError("Artifact source must be a non-hidden basename, without paths or controls.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PlayError("Artifact filename must contain valid Unicode.") from None
    return value


@dataclass(repr=False)
class Snapshot:
    stream: object = field(repr=False)
    size: int
    sha1: str
    sha256: str

    def close(self):
        self.stream.close()

    def metadata(self):
        return {"size": self.size, "sha1": self.sha1, "sha256": self.sha256}


class FileRoots:
    def __init__(self, upload_root="", download_root=""):
        self.upload_root, self.download_root = upload_root, download_root
        self._fds = {}
        self._lock = threading.Lock()

    def _root(self, kind):
        with self._lock:
            return self._open_root(kind)

    def _open_root(self, kind):
        if kind in self._fds:
            return self._fds[kind]
        path = getattr(self, kind + "_root")
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            raise PlayError("An explicit absolute artifact root directory is required.")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except (OSError, ValueError):
            raise PlayError(FILE_ERROR) from None
        self._fds[kind] = fd
        return fd

    def snapshot(self, filename, cap):
        filename = basename(filename)
        source = None
        spool = None
        try:
            fd = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._root("upload")
            )
            source = os.fdopen(fd, "rb")
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 0 < before.st_size <= cap
            ):
                raise PlayError(
                    "Artifact source must be a nonempty regular file within the configured byte limit, without hard links."
                )
            spool = tempfile.TemporaryFile(mode="w+b")
            os.fchmod(spool.fileno(), 0o600)
            size, sha1, sha256 = 0, hashlib.sha1(), hashlib.sha256()
            while chunk := source.read(CHUNK_BYTES):
                size += len(chunk)
                if size > cap:
                    raise PlayError("Artifact exceeds its effective byte limit.")
                spool.write(chunk)
                sha1.update(chunk)
                sha256.update(chunk)
            after = os.fstat(source.fileno())
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            if size != before.st_size or any(
                getattr(before, k) != getattr(after, k) for k in fields
            ):
                raise PlayError("Artifact source changed during preparation; prepare again.")
            spool.seek(0)
            result = Snapshot(spool, size, sha1.hexdigest(), sha256.hexdigest())
            spool = None
            return result
        except (OSError, ValueError):
            raise PlayError(FILE_ERROR) from None
        finally:
            if source is not None:
                source.close()
            if spool is not None:
                spool.close()

    def destination(self, cap):
        return Destination(self._root("download"), cap)

    def close(self):
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()


class Destination:
    def __init__(self, root_fd, cap):
        self.root_fd, self.cap = root_fd, cap
        self.filename = "artifact_" + secrets.token_hex(16) + ".bin"
        self.temporary = ".partial_" + secrets.token_hex(16)
        self.size, self.sha1, self.sha256 = 0, hashlib.sha1(), hashlib.sha256()
        self.stream = None
        self.published = False
        self.identity = None
        self.result = None

    def __enter__(self):
        try:
            fd = os.open(
                self.temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.root_fd,
            )
            self.stream = os.fdopen(fd, "wb")
            self.identity = os.fstat(fd)
            return self
        except OSError:
            raise PlayError(FILE_ERROR) from None

    def write(self, chunk):
        self.size += len(chunk)
        if self.size > self.cap:
            raise PlayError("Artifact download exceeds the configured byte limit.")
        self.stream.write(chunk)
        self.sha1.update(chunk)
        self.sha256.update(chunk)

    def publish(self):
        if not self.size:
            raise PlayError("Artifact download returned no bytes.")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        if not self._owned(self.temporary):
            raise PlayError(FILE_ERROR)
        # link fails if the generated final name exists; never overwrite any entry.
        os.link(
            self.temporary,
            self.filename,
            src_dir_fd=self.root_fd,
            dst_dir_fd=self.root_fd,
            follow_symlinks=False,
        )
        if not self._owned(self.filename):
            # This name was created by our successful exclusive link, but the source
            # name was replaced in the race window. Do not advertise this output.
            os.unlink(self.filename, dir_fd=self.root_fd)
            raise PlayError(FILE_ERROR)
        self.published = True
        self.result = {
            "filename": self.filename,
            "size": self.size,
            "sha1": self.sha1.hexdigest(),
            "sha256": self.sha256.hexdigest(),
        }
        return self.result

    def _owned(self, name):
        try:
            actual = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return self.identity is not None and (actual.st_dev, actual.st_ino) == (
            self.identity.st_dev,
            self.identity.st_ino,
        )

    def __exit__(self, *_):
        failed = False
        try:
            if self.stream is not None:
                self.stream.close()
        except OSError:
            failed = True
        try:
            if self._owned(self.temporary):
                os.unlink(self.temporary, dir_fd=self.root_fd)
        except OSError:
            failed = True
        if failed:
            if self.published and self.result is not None:
                self.result["cleanup_warning"] = (
                    "The completed artifact was published, but temporary file cleanup failed."
                )
            else:
                raise PlayError(FILE_ERROR)
