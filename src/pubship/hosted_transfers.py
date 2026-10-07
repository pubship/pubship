"""Connection-owned transfers backed by charged, private, unlinked disk allocations."""

import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import PlayError
from .hosted_config import TransferLimits

CHUNK_BYTES = 1024 * 1024
TRANSFER_ID = re.compile(r"transfer_[A-Za-z0-9_-]{43}\Z")
UNAVAILABLE = "Transfer storage is unavailable or could not complete the operation."
UNKNOWN = "Unknown or unavailable transfer for this connection and client."


@dataclass(frozen=True, init=False)
class TransferPurpose:
    method: str
    parameters_json: str
    mime_type: str
    capability: str
    package: str | None
    store: str | None
    target: str | None
    max_bytes: int | None

    def __init__(
        self,
        method,
        parameters,
        mime_type,
        capability,
        package=None,
        store=None,
        target=None,
        max_bytes=None,
    ):
        try:
            encoded = json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if not isinstance(parameters, dict) or len(encoded.encode()) > 256 * 1024:
                raise ValueError()
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise PlayError("Transfer purpose must contain bounded validated parameters.") from None
        if not all(isinstance(v, str) and v for v in (method, mime_type, capability)) or (
            max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0)
        ):
            raise PlayError("Invalid transfer purpose.")
        for key, value in {
            "method": method,
            "parameters_json": encoded,
            "mime_type": mime_type,
            "capability": capability,
            "package": package,
            "store": store,
            "target": target,
            "max_bytes": max_bytes,
        }.items():
            object.__setattr__(self, key, value)

    @property
    def parameters(self):
        return json.loads(self.parameters_json)


@dataclass(repr=False)
class _Record:
    owner: tuple[str, str]
    purpose: TransferPurpose
    direction: str
    cap: int
    charge: int
    deadline: float
    expected_hash: str | None
    stream: object = None
    state: str = "RESERVED"
    size: int = 0
    sha1: object = field(default_factory=hashlib.sha1)
    sha256: object = field(default_factory=hashlib.sha256)
    active: bool = False
    busy: int = 0
    invalid: bool = False
    cleanup_pending: bool = False
    pending_name: str | None = None
    io_lock: object = field(default_factory=threading.Lock)


class HostedTransferStore:
    """All live and cleanup-pending allocations remain in one quota pool.

    Network authorization runs outside allocator locks. No lock crosses an await.
    Disk cleanup is detached from the allocator lock and retried in-place.
    """

    def __init__(self, directory=None, limits=None, *, _allocate=None, cleanup_interval=30):
        self.limits = limits or TransferLimits()
        self._allocate = _allocate or getattr(os, "posix_fallocate", None)
        self._test_named_spools = _allocate is not None and not hasattr(os, "O_TMPFILE")
        self._records = {}
        self._lock = threading.Lock()
        self._closed = False
        self._root = None
        self._stop = threading.Event()
        if directory is not None:
            self._root = self._open_root(directory)
            if self._allocate is None:
                os.close(self._root)
                self._root = None
                raise PlayError(
                    "Hosted transfer storage requires a filesystem with physical allocation support."
                )
        if (
            not isinstance(cleanup_interval, (int, float))
            or not math.isfinite(cleanup_interval)
            or cleanup_interval <= 0
        ):
            raise PlayError("Invalid transfer cleanup interval.")
        self._worker = threading.Thread(
            target=self._clean_loop,
            args=(cleanup_interval,),
            daemon=True,
            name="hosted-transfer-cleanup",
        )
        self._worker.start()

    @staticmethod
    def _open_root(path):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise PlayError("Hosted transfer storage requires an absolute private directory.")
        fd = None
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise OSError()
            return fd
        except (OSError, ValueError):
            if fd is not None:
                os.close(fd)
            raise PlayError(
                "Hosted transfer directory must be private, owned and free of symlinks."
            ) from None

    @property
    def enabled(self):
        return self._root is not None and not self._closed

    def configuration(self):
        return {
            "enabled": self.enabled,
            **asdict(self.limits),
            "storage": "private_unlinked_disk",
            "lost_on_restart": True,
        }

    @staticmethod
    def _owner(owner):
        if (
            not isinstance(owner, tuple)
            or len(owner) != 2
            or not all(isinstance(v, str) and v for v in owner)
        ):
            raise PlayError("Authenticated connection and client ownership is required.")
        return owner

    def _record(self, owner, transfer_id):
        self._owner(owner)
        if not isinstance(transfer_id, str) or not TRANSFER_ID.fullmatch(transfer_id):
            raise PlayError(UNKNOWN)
        record = self._records.get(transfer_id)
        if record is None or record.owner != owner:
            raise PlayError(UNKNOWN)
        return record

    def _valid(self, record):
        if self._closed or record.invalid or time.monotonic() >= record.deadline:
            raise PlayError("Transfer expired or was disposed; start a new transfer.")

    def purpose(self, owner, transfer_id):
        with self._lock:
            record = self._record(owner, transfer_id)
            self._valid(record)
            return record.purpose

    def status(self, owner, transfer_id):
        with self._lock:
            record = self._record(owner, transfer_id)
            self._valid(record)
            return self._status(transfer_id, record)

    def _status(self, transfer_id, record):
        return {
            "transfer_id": transfer_id,
            "state": record.state,
            "direction": record.direction,
            "size_bytes": record.size,
            "maximum_bytes": record.cap,
            "mime_type": record.purpose.mime_type,
            "expires_in_seconds": max(0, int(record.deadline - time.monotonic())),
            "content_path": f"/transfers/{transfer_id}/content",
            "sha256": record.sha256.hexdigest()
            if record.state in {"READY", "CLAIMED", "DOWNLOAD_READY"}
            else None,
        }

    def _reserve(self, owner, purpose, size, deadline, direction, sha256=None):
        owner = self._owner(owner)
        if (
            not isinstance(purpose, TransferPurpose)
            or type(size) is not int
            or size <= 0
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise PlayError("Invalid transfer reservation.")
        if not self.enabled:
            raise PlayError("Transfer storage is not configured or service is shutting down.")
        self.cleanup()
        failed = None
        with self._lock:
            if not self.enabled:
                raise PlayError(UNAVAILABLE)
            owned = [r for r in self._records.values() if r.owner == owner]
            if (
                len(owned) >= self.limits.connection_handles
                or len(self._records) >= self.limits.total_handles
            ):
                raise PlayError("Transfer handle capacity reached; discard unused transfers.")
            try:
                disk = os.fstatvfs(self._root)
                unit = max(disk.f_frsize or disk.f_bsize, 512)
                available = disk.f_bavail * disk.f_frsize - self.limits.free_disk_bytes
                maximum = min(
                    self.limits.connection_bytes - sum(r.charge for r in owned),
                    self.limits.total_bytes - sum(r.charge for r in self._records.values()),
                    available,
                )
                charge = (size + unit - 1) // unit * unit
                if (
                    size
                    > min(self.limits.object_bytes, purpose.max_bytes or self.limits.object_bytes)
                    or charge > maximum
                ):
                    raise PlayError(
                        "Transfer exceeds effective object, connection, service or disk capacity."
                    )
                effective_deadline = min(deadline, time.monotonic() + self.limits.ttl_seconds)
                if effective_deadline <= time.monotonic():
                    raise PlayError("Transfer authorization already expired.")
                transfer_id = "transfer_" + secrets.token_urlsafe(32)
                while transfer_id in self._records:
                    transfer_id = "transfer_" + secrets.token_urlsafe(32)
                record = _Record(
                    owner, purpose, direction, size, charge, effective_deadline, sha256
                )
                self._records[transfer_id] = record
                try:
                    if self._test_named_spools:
                        # Explicit non-Linux test backend only. Production uses
                        # anonymous O_TMPFILE; failed test unlink stays charged.
                        name = ".transfer-" + secrets.token_hex(24)
                        fd = os.open(
                            name,
                            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=self._root,
                        )
                        record.pending_name = name
                    else:
                        fd = os.open(".", os.O_RDWR | os.O_TMPFILE, 0o600, dir_fd=self._root)
                    try:
                        record.stream = os.fdopen(fd, "w+b")
                    except BaseException:
                        os.close(fd)
                        raise
                    if record.pending_name is not None:
                        os.unlink(record.pending_name, dir_fd=self._root)
                        record.pending_name = None
                    self._allocate(record.stream.fileno(), 0, charge)
                    after = os.fstatvfs(self._root)
                    if after.f_bavail * after.f_frsize < self.limits.free_disk_bytes:
                        raise OSError()
                except BaseException as error:
                    record.invalid = True
                    record.state = "DISPOSED"
                    failed = (transfer_id, record, error)
            except OSError:
                raise PlayError(UNAVAILABLE) from None
        if failed:
            self._dispose_record(failed[0], failed[1])
            if isinstance(failed[2], (KeyboardInterrupt, SystemExit)):
                raise failed[2]
            raise PlayError(UNAVAILABLE) from None
        return transfer_id, record

    def reserve_upload(self, owner, purpose, size, sha256, deadline):
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise PlayError("Upload requires an exact lowercase SHA-256 digest.")
        transfer_id, record = self._reserve(owner, purpose, size, deadline, "upload", sha256)
        return self._status(transfer_id, record)

    def reserve_download(self, owner, purpose, limit, deadline, *, authorize=None):
        transfer_id, record = self._reserve(owner, purpose, limit, deadline, "download")
        return DestinationLease(self, transfer_id, record, authorize)

    def _activate(self, record):
        self._valid(record)
        if record.active:
            raise PlayError("Transfer is busy.")
        active = [r for r in self._records.values() if r.active]
        if (
            len(active) >= self.limits.total_streams
            or sum(r.owner == record.owner for r in active) >= self.limits.connection_streams
        ):
            raise PlayError("Transfer stream capacity reached; finish the current stream.")
        record.active = True

    def begin_receive(self, owner, transfer_id, *, authorize=None):
        if authorize:
            authorize()
        with self._lock:
            record = self._record(owner, transfer_id)
            self._valid(record)
            if record.direction != "upload" or record.state != "RESERVED":
                raise PlayError("Upload is not reserved; inspect its status before retrying.")
            self._activate(record)
            record.state = "RECEIVING"
        return UploadLease(self, transfer_id, record, authorize)

    def claim_upload(self, owner, purpose, transfer_id, *, authorize=None):
        if authorize:
            authorize()
        with self._lock:
            record = self._record(owner, transfer_id)
            self._valid(record)
            if record.purpose != purpose or record.direction != "upload" or record.state != "READY":
                raise PlayError("Upload is not ready for this exact operation and target.")
            record.state = "CLAIMED"
        return SnapshotLease(self, transfer_id, record, authorize)

    def open_download(self, owner, transfer_id, *, authorize=None):
        if authorize:
            authorize()
        with self._lock:
            record = self._record(owner, transfer_id)
            self._valid(record)
            if record.direction != "download" or record.state != "DOWNLOAD_READY":
                raise PlayError("Download is not ready.")
            self._activate(record)
        lease = DownloadLease(self, transfer_id, record, authorize)
        try:
            lease.seek(0)
        except BaseException:
            lease.close()
            raise
        return lease

    def _io(self, transfer_id, record, authorize, operation):
        if authorize:
            authorize()
        with self._lock:
            self._valid(record)
            record.busy += 1
        try:
            while True:
                # Wait for previous disk work, then reauthorize without holding
                # an allocator or I/O lock. A nonblocking claim closes the gap
                # where another operation could queue us behind new disk work.
                remaining = record.deadline - time.monotonic()
                if remaining <= 0 or not record.io_lock.acquire(timeout=remaining):
                    raise PlayError("Transfer expired while waiting for disk access.")
                record.io_lock.release()
                if authorize:
                    authorize()
                if record.io_lock.acquire(blocking=False):
                    break
            try:
                with self._lock:
                    self._valid(record)
                result = operation()
            finally:
                record.io_lock.release()
            # Do not hand newly read bytes to a caller after authority changed
            # while disk I/O was in progress. Already-sent bytes are irrevocable.
            if authorize:
                authorize()
            with self._lock:
                self._valid(record)
            return result
        except (OSError, ValueError):
            self._dispose_record(transfer_id, record)
            raise PlayError(UNAVAILABLE) from None
        finally:
            with self._lock:
                record.busy -= 1
                pending = record.invalid
            if pending:
                self._dispose_record(transfer_id, record)

    def _dispose_record(self, transfer_id, record):
        with self._lock:
            if self._records.get(transfer_id) is not record:
                return "disposed"
            record.invalid = True
            record.state = "DISPOSED"
            if record.busy:
                return "deferred-busy"
            record.busy += 1
        succeeded = False
        try:
            with record.io_lock:
                if record.stream is not None:
                    record.stream.close()
                if record.pending_name is not None:
                    try:
                        os.unlink(record.pending_name, dir_fd=self._root)
                    except FileNotFoundError:
                        pass
                    record.pending_name = None
            succeeded = True
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                record.busy -= 1
                record.cleanup_pending = not succeeded
                if succeeded:
                    record.active = False
                    self._records.pop(transfer_id, None)
        return "disposed" if succeeded else "cleanup-pending"

    def discard(self, owner, transfer_id):
        with self._lock:
            record = self._record(owner, transfer_id)
        return self._dispose_record(transfer_id, record)

    def invalidate(self, subject):
        with self._lock:
            records = [
                (key, record) for key, record in self._records.items() if record.owner[0] == subject
            ]
        for key, record in records:
            self._dispose_record(key, record)

    def cleanup(self):
        with self._lock:
            records = [
                (key, record)
                for key, record in self._records.items()
                if record.invalid or time.monotonic() >= record.deadline
            ]
        for key, record in records:
            self._dispose_record(key, record)

    def _clean_loop(self, interval):
        while not self._stop.wait(interval):
            self.cleanup()

    def close(self):
        self._stop.set()
        with self._lock:
            self._closed = True
            records = list(self._records.items())
        for key, record in records:
            self._dispose_record(key, record)
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=1)
        with self._lock:
            if self._root is not None and not self._records:
                os.close(self._root)
                self._root = None


class _Lease:
    def __init__(self, store, transfer_id, record, authorize):
        self._store, self._id, self._record, self._authorize = store, transfer_id, record, authorize
        self.deadline = record.deadline
        self._released = False

    @property
    def size(self):
        return self._record.size

    @property
    def sha1(self):
        return self._record.sha1.hexdigest()

    @property
    def sha256(self):
        return self._record.sha256.hexdigest()

    def metadata(self):
        return {"size": self.size, "sha1": self.sha1, "sha256": self.sha256}

    def dispose(self):
        return self._store._dispose_record(self._id, self._record)

    def close(self):
        return self.dispose()

    def validate(self):
        """Recheck current authority and the original lease before dispatching bytes."""
        if self._released:
            raise PlayError("Transfer lease has been released.")
        if self._authorize:
            self._authorize()
        with self._store._lock:
            self._store._valid(self._record)

    def _io(self, operation):
        if self._released:
            raise PlayError("Transfer lease has been released.")
        return self._store._io(self._id, self._record, self._authorize, operation)


class UploadLease(_Lease):
    def write(self, chunk):
        if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
            raise PlayError("Transfer chunks must be bounded bytes.")

        def write():
            record = self._record
            if record.state != "RECEIVING":
                raise PlayError("Transfer is not receiving bytes.")
            if record.size + len(chunk) > record.cap:
                raise PlayError("Transfer exceeds its reserved byte length.")
            disk = os.fstatvfs(self._store._root)
            if disk.f_bavail * disk.f_frsize < self._store.limits.free_disk_bytes:
                raise PlayError("Transfer disk headroom is exhausted.")
            if record.stream.write(chunk) != len(chunk):
                raise OSError()
            record.size += len(chunk)
            record.sha1.update(chunk)
            record.sha256.update(chunk)

        return self._io(write)

    def seal(self):
        def finish():
            record = self._record
            if record.state != "RECEIVING":
                raise PlayError("Transfer is not receiving bytes.")
            if record.size != record.cap or record.sha256.hexdigest() != record.expected_hash:
                raise PlayError("Upload length or SHA-256 does not match its reservation.")
            record.stream.flush()
            os.fsync(record.stream.fileno())
            record.stream.seek(0)
            with self._store._lock:
                self._store._valid(record)
                record.state = "READY"
                record.active = False
                return self._store._status(self._id, record)

        return self._io(finish)


class SnapshotLease(_Lease):
    @property
    def stream(self):
        return self

    def seek(self, offset, whence=0):
        if offset != 0 or whence != 0:
            raise PlayError("A sealed transfer only supports rewind before delivery.")

        def rewind():
            with self._store._lock:
                if not self._record.active:
                    self._store._activate(self._record)
            return self._record.stream.seek(0)

        return self._io(rewind)

    def read(self, amount=CHUNK_BYTES):
        if type(amount) is not int or amount < 0:
            amount = CHUNK_BYTES
        amount = min(amount, CHUNK_BYTES)

        def read():
            with self._store._lock:
                if not self._record.active:
                    self._store._activate(self._record)
            remaining = self.size - self._record.stream.tell()
            return self._record.stream.read(min(amount, max(0, remaining)))

        return self._io(read)


class DownloadLease(SnapshotLease):
    def close(self):
        self._released = True
        with self._store._lock:
            self._record.active = False
            invalid = self._record.invalid
        return self.dispose() if invalid else "disposed"


class DestinationLease(UploadLease):
    @property
    def cap(self):
        return self._record.cap

    def __enter__(self):
        if self._authorize:
            self._authorize()
        with self._store._lock:
            if self._record.state != "RESERVED":
                raise PlayError("Download destination was already used.")
            self._store._activate(self._record)
            self._record.state = "RECEIVING"
        return self

    def publish(self):
        def finish():
            record = self._record
            if not record.size:
                raise PlayError("Download returned no bytes.")
            record.stream.flush()
            record.stream.truncate(record.size)
            os.fsync(record.stream.fileno())
            disk = os.fstatvfs(self._store._root)
            unit = max(disk.f_frsize or disk.f_bsize, 512)
            with self._store._lock:
                self._store._valid(record)
                record.charge = (record.size + unit - 1) // unit * unit
                record.state = "DOWNLOAD_READY"
                record.active = False
                return self._store._status(self._id, record) | self.metadata()

        return self._io(finish)

    def __exit__(self, exc_type, *_):
        if exc_type is not None or self._record.state != "DOWNLOAD_READY":
            self.dispose()
