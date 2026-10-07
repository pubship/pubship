"""Transfer ownership, allocator accounting and bounded leases; no provider calls."""

import hashlib
import os
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pubship.errors import PlayError
from pubship.hosted_config import TransferLimits
from pubship.hosted_transfers import HostedTransferStore, TransferPurpose

OWNER = ("connection-a", "client-a")
PURPOSE = TransferPurpose(
    "androidpublisher.edits.apks.upload",
    {"packageName": "com.example.app", "editId": "edit-1"},
    "application/octet-stream",
    "play:edit-artifacts",
    package="com.example.app",
    max_bytes=10 * 1024**3,
)
DATA = b"synthetic binary data"


def limits(**values):
    return replace(
        TransferLimits(
            object_bytes=4096,
            connection_bytes=8192,
            total_bytes=16384,
            ttl_seconds=30,
            stream_timeout_seconds=30,
            idle_timeout_seconds=1,
        ),
        **values,
    )


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    # Explicit fake physical allocator for macOS unit tests. A separate Linux
    # test below proves posix_fallocate and allocated blocks with a small file.
    value = HostedTransferStore(
        tmp_path.resolve(), limits(), _allocate=lambda fd, off, size: os.ftruncate(fd, size)
    )
    yield value
    value.close()


def reserve(store, *, owner=OWNER, purpose=PURPOSE, data=DATA):
    return store.reserve_upload(
        owner, purpose, len(data), hashlib.sha256(data).hexdigest(), time.monotonic() + 60
    )["transfer_id"]


def ready(store, *, owner=OWNER, purpose=PURPOSE, data=DATA):
    handle = reserve(store, owner=owner, purpose=purpose, data=data)
    receiving = store.begin_receive(owner, handle)
    receiving.write(data)
    result = receiving.seal()
    assert result["state"] == "READY"
    return handle


def test_purpose_is_immutable_after_caller_mutation():
    parameters = {"packageName": "com.example.app", "nested": {"value": ["one"]}}
    purpose = TransferPurpose(
        PURPOSE.method, parameters, PURPOSE.mime_type, PURPOSE.capability, package=PURPOSE.package
    )
    parameters["nested"]["value"].append("two")
    purpose.parameters["nested"]["value"].append("three")
    assert purpose.parameters["nested"]["value"] == ["one"]


def test_upload_claim_is_single_use_exact_purpose_and_no_paths(store):
    handle = ready(store)
    for owner in (("other", "client-a"), ("connection-a", "other")):
        with pytest.raises(PlayError, match="Unknown"):
            store.claim_upload(owner, PURPOSE, handle)
    other = TransferPurpose(
        PURPOSE.method,
        {"packageName": "com.example.app", "editId": "other-edit"},
        PURPOSE.mime_type,
        PURPOSE.capability,
        package=PURPOSE.package,
    )
    with pytest.raises(PlayError, match="exact operation"):
        store.claim_upload(OWNER, other, handle)
    lease = store.claim_upload(OWNER, PURPOSE, handle)
    with pytest.raises(PlayError):
        store.claim_upload(OWNER, PURPOSE, handle)
    assert lease.metadata() == {
        "size": len(DATA),
        "sha1": hashlib.sha1(DATA).hexdigest(),
        "sha256": hashlib.sha256(DATA).hexdigest(),
    }
    lease.stream.seek(0)
    assert lease.stream.read() == DATA
    assert lease.stream.read() == b""
    assert lease.close() == "disposed"
    assert lease.close() == "disposed"
    assert not store._records


@pytest.mark.parametrize("corruption", ["short", "long", "hash"])
def test_upload_mismatch_cannot_be_sealed(store, corruption):
    handle = reserve(store)
    lease = store.begin_receive(OWNER, handle)
    data = (
        DATA[:-1]
        if corruption == "short"
        else DATA + b"!"
        if corruption == "long"
        else b"x" * len(DATA)
    )
    with pytest.raises(PlayError):
        lease.write(data)
        lease.seal()
    assert store.status(OWNER, handle)["state"] != "READY"
    assert lease.dispose() == "disposed"


def test_download_reservation_shrinks_after_seal_and_repeat_delivery_uses_same_file(store):
    purpose = TransferPurpose(
        "androidpublisher.generatedapks.download",
        {"packageName": "com.example.app", "versionCode": 1, "downloadId": "synthetic"},
        "application/octet-stream",
        "play:artifact-downloads",
        package="com.example.app",
    )
    destination = store.reserve_download(OWNER, purpose, 4096, time.monotonic() + 30)
    with destination:
        destination.write(DATA)
        result = destination.publish()
    handle = result["transfer_id"]
    assert result["state"] == "DOWNLOAD_READY" and result["size"] == len(DATA)
    assert set(result).isdisjoint({"path", "filename", "root", "stream"})
    for _ in range(2):
        delivery = store.open_download(OWNER, handle)
        assert delivery.read(4) == DATA[:4]
        assert delivery.read() == DATA[4:]
        delivery.close()
        with pytest.raises(PlayError, match="released"):
            delivery.read()
    assert store.discard(OWNER, handle) == "disposed"


def test_declared_reservation_accounts_all_states_and_shares_owner_quota(store):
    handle = reserve(store)
    assert store._records[handle].charge == 4096
    second = reserve(store)
    with pytest.raises(PlayError, match="capacity"):
        reserve(store)
    store.discard(OWNER, second)
    lease = store.begin_receive(OWNER, handle)
    lease.write(DATA)
    lease.seal()
    claimed = store.claim_upload(OWNER, PURPOSE, handle)
    assert store._records[handle].charge == 4096
    second = reserve(store)
    with pytest.raises(PlayError, match="capacity"):
        reserve(store)
    claimed.dispose()
    store.discard(OWNER, second)
    assert reserve(store)


def test_stream_limits_apply_before_upload_body_and_across_owners(store):
    first, second = reserve(store), reserve(store)
    one = store.begin_receive(OWNER, first)
    with pytest.raises(PlayError, match="stream capacity"):
        store.begin_receive(OWNER, second)
    other_owner = ("other-connection", "other-client")
    other = reserve(store, owner=other_owner)
    assert store.begin_receive(other_owner, other)
    one.dispose()
    assert store.begin_receive(OWNER, second)


def test_discard_during_busy_io_defers_close_then_stops_next_chunk(store):
    handle = ready(store)
    lease = store.claim_upload(OWNER, PURPOSE, handle)
    entered, release = threading.Event(), threading.Event()
    result = []

    def blocked():
        entered.set()
        release.wait(2)
        return b"already in flight"

    def run():
        try:
            result.append(lease._io(blocked))
        except PlayError:
            result.append("denied")

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(1)
    assert store.discard(OWNER, handle) == "deferred-busy"
    assert handle in store._records
    release.set()
    thread.join(2)
    assert result == ["denied"]
    assert handle not in store._records
    with pytest.raises(PlayError):
        lease.read()


def test_failed_close_keeps_quota_until_cleanup_succeeds(store):
    handle = ready(store)
    record = store._records[handle]
    original = record.stream
    calls = [0]

    def close():
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("synthetic private path must not leak")
        original.close()

    record.stream = SimpleNamespace(close=close)
    assert store.discard(OWNER, handle) == "cleanup-pending"
    assert record.charge == 4096 and handle in store._records
    store.cleanup()
    assert handle not in store._records and calls[0] == 2


def test_original_deadline_does_not_extend_when_new_authorization_succeeds(store):
    handle = ready(store)
    lease = store.claim_upload(OWNER, PURPOSE, handle, authorize=lambda: None)
    store._records[handle].deadline = time.monotonic()
    with pytest.raises(PlayError, match="expired"):
        lease.read()
    store.cleanup()
    assert handle not in store._records


def test_current_authorization_rechecked_at_each_stream_operation(store):
    handle = ready(store)
    auth = Mock()
    lease = store.claim_upload(OWNER, PURPOSE, handle, authorize=auth)
    lease.seek(0)
    assert lease.read(1) == DATA[:1]
    auth.side_effect = PlayError("Authority narrowed")
    with pytest.raises(PlayError, match="narrowed"):
        lease.read()
    assert auth.call_count == 8
    lease.dispose()


def test_revoke_clears_only_target_connection_and_restart_loses_ids(store):
    first = ready(store)
    other_owner = ("connection-b", "client-a")
    second = ready(store, owner=other_owner)
    store.invalidate(OWNER[0])
    assert first not in store._records
    assert store.status(other_owner, second)["state"] == "READY"
    store.close()
    assert not store._records


def test_unsupported_allocation_fails_closed_without_surviving_reservation(tmp_path):
    tmp_path.chmod(0o700)

    def fail(fd, offset, size):
        raise OSError("unsupported allocation private disk")

    store = HostedTransferStore(tmp_path.resolve(), limits(), _allocate=fail)
    try:
        with pytest.raises(PlayError, match="unavailable"):
            reserve(store)
        assert not store._records and list(tmp_path.iterdir()) == []
    finally:
        store.close()


def test_above_one_gib_is_configurable_without_giant_memory_allocation(tmp_path):
    tmp_path.chmod(0o700)
    size = 1024**3 + 4096
    allocation = Mock()  # Accounting fixture only, not evidence of physical allocation.
    store = HostedTransferStore(
        tmp_path.resolve(),
        limits(object_bytes=size, connection_bytes=2 * size, total_bytes=4 * size),
        _allocate=allocation,
    )
    try:
        status = store.reserve_upload(OWNER, PURPOSE, size, "0" * 64, time.monotonic() + 30)
        assert status["maximum_bytes"] == size
        assert allocation.call_args.args[-1] == size
    finally:
        store.close()


def test_private_symlink_storage_and_absent_storage_fail_closed(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PlayError, match="private"):
        HostedTransferStore(link.resolve().parent / "link", _allocate=lambda *args: None)
    store = HostedTransferStore()
    try:
        assert store.configuration()["enabled"] is False
        with pytest.raises(PlayError, match="not configured"):
            reserve(store)
    finally:
        store.close()


@pytest.mark.skipif(
    not hasattr(os, "posix_fallocate"), reason="Physical reservation requires Linux posix_fallocate"
)
def test_small_real_physical_allocation_is_unlinked_and_reclaimed(tmp_path):
    tmp_path.chmod(0o700)
    store = HostedTransferStore(tmp_path.resolve(), limits())
    try:
        handle = reserve(store)
        record = store._records[handle]
        info = os.fstat(record.stream.fileno())
        assert info.st_nlink == 0 and info.st_blocks * 512 >= record.charge
        assert list(tmp_path.iterdir()) == []
        stream = record.stream
        assert store.discard(OWNER, handle) == "disposed"
        assert stream.closed and not store._records
    finally:
        store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"object_bytes": True},
        {"connection_bytes": 1},
        {"total_handles": 1},
        {"free_disk_bytes": 0},
        {"ttl_seconds": 0},
        {"ttl_seconds": 601, "stream_timeout_seconds": 600},
        {"idle_timeout_seconds": 601},
    ],
)
def test_inconsistent_configuration_rejected(changes):
    with pytest.raises(PlayError):
        replace(TransferLimits(), **changes)


@pytest.mark.parametrize("phase", ["waiting", "reading"])
def test_revocation_while_waiting_or_reading_never_returns_bytes(store, phase):
    handle = ready(store)
    entered, release = threading.Event(), threading.Event()
    allowed = [True]
    calls = []

    def authorize():
        if not allowed[0]:
            raise PlayError("Revoked")
        entered.set()

    lease = store.claim_upload(OWNER, PURPOSE, handle, authorize=authorize)
    entered.clear()
    record = store._records[handle]

    def operation():
        calls.append("read")
        if phase == "reading":
            entered.set()
            assert release.wait(2)
        return b"private bytes"

    results = []

    def run():
        try:
            results.append(lease._io(operation))
        except PlayError:
            results.append("denied")

    if phase == "waiting":
        record.io_lock.acquire()
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(1)
    allowed[0] = False
    release.set()
    if phase == "waiting":
        record.io_lock.release()
    thread.join(2)
    assert not thread.is_alive()
    assert results == ["denied"]
    assert calls == ([] if phase == "waiting" else ["read"])


def test_failed_unlink_remains_charged_until_cleanup(store, monkeypatch):
    store._test_named_spools = True
    original = os.unlink
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", Mock(side_effect=OSError("private path")))
        with pytest.raises(PlayError):
            reserve(store)
        assert len(store._records) == 1
        record = next(iter(store._records.values()))
        assert record.pending_name and record.charge == 4096 and record.cleanup_pending
    store.cleanup()
    assert not store._records
    assert original is os.unlink


def test_disk_lock_wait_respects_original_deadline(store):
    handle = ready(store)
    lease = store.claim_upload(OWNER, PURPOSE, handle)
    record = store._records[handle]
    record.deadline = time.monotonic() + 0.02
    record.io_lock.acquire()
    try:
        with pytest.raises(PlayError, match="expired"):
            lease.read(1)
    finally:
        record.io_lock.release()
