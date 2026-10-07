"""Bounded launch admission, byte isolation, legacy migration and full-vault recovery."""

import sqlite3
import statistics
import time
import tracemalloc
from contextlib import closing
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from test_hosted import hosted as hosted  # noqa: F401

from pubship.errors import PlayError
from pubship.hosted import create_hosted_app
from pubship.vault import Vault, VaultLimits
from pubship.vault_recovery import MAX_BYTES, MAX_RECORDS, backup, recover


@pytest.fixture
def store(tmp_path):
    tmp_path.chmod(0o700)
    key = Fernet.generate_key()
    vault = Vault(tmp_path / "vault.db", key)
    yield vault, key
    vault.close()


def grant(vault, owner):
    vault.put("grants", owner, {"client_id": "client-" + owner}, 3600)


@pytest.mark.parametrize("value", [0, -1, 33, 128, True, 1.5, "32", None])
def test_invalid_connection_configuration_rejected(value):
    with pytest.raises(PlayError):
        VaultLimits(connections=value)


@pytest.mark.parametrize(
    "value", ["0", "33", "128", "-1", "+1", "1.5", " 2", "", "true", "３２", "032"]
)
def test_invalid_environment_rejected(monkeypatch, value):
    monkeypatch.setenv("PUBSHIP_SERVER_VAULT_CONNECTIONS", value)
    with pytest.raises(PlayError):
        VaultLimits.from_env()


@pytest.mark.parametrize("value, expected", [(None, 32), ("1", 1), ("16", 16), ("32", 32)])
def test_configuration_default_and_supported_envelopes(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PUBSHIP_SERVER_VAULT_CONNECTIONS", raising=False)
    else:
        monkeypatch.setenv("PUBSHIP_SERVER_VAULT_CONNECTIONS", value)
    assert VaultLimits.from_env().connections == expected


def test_configured_limit_reaches_real_hosted_vault(hosted, tmp_path):  # noqa: F811
    settings, _, _ = hosted
    app = create_hosted_app(
        replace(settings, database=tmp_path / "configured.db", vault_limits=VaultLimits(2))
    )
    try:
        grant(app.state.vault, "first")
        grant(app.state.vault, "second")
        with pytest.raises(PlayError, match="capacity"):
            grant(app.state.vault, "third")
    finally:
        app.state.vault.close()


def test_default_admits_32_complete_reservations_and_reclaims_disconnect(store):
    vault, _ = store
    for number in range(32):
        grant(vault, str(number))
    with pytest.raises(PlayError, match="capacity"):
        grant(vault, "overflow")
    assert vault.get("grants", "overflow") is None
    usage = vault._usage()
    assert usage.reserved == 32 * 4096
    assert usage.reserved_bytes == 32 * 4 * 1024**2
    vault.delete_connection("0")
    grant(vault, "replacement")
    assert vault.get("grants", "1") == {"client_id": "client-1"}


def test_byte_full_family_keeps_records_and_cannot_spend_neighbors_reservation(store):
    vault, _ = store
    for number in range(32):
        grant(vault, str(number))
    large = {"subject": "0", "client_id": "client-0", "padding": "x" * 60000}
    admitted = 0
    for number in range(100):
        try:
            vault.put("used_refresh", str(number), large, 3600)
            admitted += 1
        except PlayError:
            break
    else:
        pytest.fail("Byte budget was not enforced")
    assert 1 < admitted < 100
    assert vault.get("used_refresh", str(admitted)) is None
    assert vault.get("used_refresh", "0") == large
    assert vault.get("grants", "0") is not None
    vault.put("access", "neighbor-access", {"subject": "1", "client_id": "client-1"}, 600)
    assert vault.get("access", "neighbor-access") is not None
    assert vault._usage().owner_bytes[Vault.digest("0")] <= Vault.CONNECTION_BYTES


@pytest.mark.parametrize("namespace", ["clients", "consent", "google_state"])
def test_admission_byte_exhaustion_is_separate_and_atomic(store, namespace):
    vault, _ = store
    grant(vault, "existing")
    with pytest.raises(PlayError, match="capacity"):
        vault.put_many(
            [(namespace, str(index), {"padding": "x" * 60000}, 600) for index in range(120)]
        )
    assert vault.get(namespace, "0") is None
    assert vault.get("grants", "existing") is not None
    vault.put("access", "existing-access", {"subject": "existing"}, 600)
    assert vault.get("access", "existing-access") is not None


def test_lower_envelope_grandfathers_live_families_without_new_admission(store, tmp_path):
    vault, key = store
    for number in range(3):
        grant(vault, str(number))
    vault.close()
    with closing(Vault(tmp_path / "vault.db", key, limits=VaultLimits(2))) as reduced:
        assert all(reduced.get("grants", str(number)) for number in range(3))
        reduced.put("access", "existing-access", {"subject": "0"}, 600)
        with pytest.raises(PlayError, match="capacity"):
            grant(reduced, "new")
        reduced.delete_connection("1")
        with pytest.raises(PlayError, match="capacity"):
            grant(reduced, "new")
        reduced.delete_connection("2")
        grant(reduced, "new")


def test_byte_overbudget_legacy_rows_survive_restart_but_cannot_grow(store, monkeypatch, tmp_path):
    vault, key = store
    grant(vault, "legacy")
    value = {"subject": "legacy", "client_id": "client-legacy", "padding": "x" * 60000}
    with monkeypatch.context() as larger:
        larger.setattr(Vault, "CONNECTION_BYTES", 8 * 1024**2)
        vault.put_many([("used_refresh", str(index), value, 3600) for index in range(60)])
    before = vault._usage().owner_bytes[Vault.digest("legacy")]
    assert before > Vault.CONNECTION_BYTES
    vault.close()
    with closing(Vault(tmp_path / "vault.db", key)) as restored:
        assert restored.get("used_refresh", "0") == value
        restored.put("used_refresh", "0", value, 3600)
        with pytest.raises(PlayError, match="capacity"):
            restored.put("used_refresh", "new", {"subject": "legacy"}, 600)
        grant(restored, "neighbor")
        assert restored.get("used_refresh", "0") == value


def test_database_page_ceiling_and_disk_full_rollback(store):
    vault, _ = store
    page_size = vault.db.execute("PRAGMA page_size").fetchone()[0]
    assert (
        vault.db.execute("PRAGMA max_page_count").fetchone()[0] * page_size == Vault.DATABASE_BYTES
    )
    assert Vault.DATABASE_BYTES < MAX_BYTES
    grant(vault, "kept")
    pages = vault.db.execute("PRAGMA page_count").fetchone()[0]
    vault.db.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(PlayError, match="storage"):
        vault.put_many(
            [("clients", str(index), {"padding": "x" * 60000}, 600) for index in range(10)]
        )
    assert vault.get("grants", "kept") == {"client_id": "client-kept"}
    assert vault.get("clients", "0") is None


def test_full_launch_vault_aggregate_memory_restart_and_recovery(tmp_path):
    """Exercise 131,072 authenticated rows, not a count-only or empty-vault claim."""
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    source, snapshot, output, key_file = (
        tmp_path / name for name in ("full.db", "backup.db", "empty.db", "key")
    )
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    key_file.chmod(0o600)
    with closing(Vault(source, key)) as vault:
        for owner in range(Vault.MAX_CONNECTIONS):
            subject = f"family-{owner}"
            vault.put_many(
                [("grants", subject, {"client_id": f"client-{owner}"}, 3600)]
                + [
                    (
                        "used_refresh",
                        f"{owner}-{index}",
                        {"subject": subject, "client_id": f"client-{owner}"},
                        3600,
                    )
                    for index in range(4095)
                ]
            )
        already_tracing = tracemalloc.is_tracing()
        if not already_tracing:
            tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            start = tracemalloc.get_traced_memory()[0]
            usage = vault._usage()
            peak = tracemalloc.get_traced_memory()[1] - start
            assert peak < 4 * 1024**2, f"Aggregate Python memory grew to {peak} bytes"
        finally:
            if not already_tracing:
                tracemalloc.stop()
        # A machine-independent SQLite VM budget catches reintroduced global
        # scans. Measure real admission transactions at full encrypted occupancy
        # too; keep elapsed values as evidence, not timing-sensitive assertions.
        steps = 0

        def bounded_steps():
            nonlocal steps
            steps += 100
            return int(steps > 10000)

        vault.db.set_progress_handler(bounded_steps, 100)
        try:
            assert vault._usage() == usage
        finally:
            vault.db.set_progress_handler(None, 0)
        aggregate_times, admission_times = [], []
        for _ in range(5):
            started = time.perf_counter()
            vault._usage()
            aggregate_times.append(time.perf_counter() - started)
            started = time.perf_counter()
            vault.put("clients", "timed-registration", {}, 600)
            admission_times.append(time.perf_counter() - started)
            vault.delete("clients", "timed-registration")
        print(
            f"full occupancy aggregate median={statistics.median(aggregate_times):.6f}s "
            f"admission median={statistics.median(admission_times):.6f}s sqlite_steps<={steps}"
        )
        assert_accounting(vault)
        assert usage.actual == 131072 and len(usage.owners) == 32
        assert usage.actual_bytes <= 32 * Vault.CONNECTION_BYTES
        assert MAX_RECORDS >= usage.actual + sum(Vault.ADMISSION_LIMITS.values())
        with pytest.raises(PlayError, match="capacity"):
            vault.put("used_refresh", "overflow", {"subject": "family-0"}, 3600)
    with closing(Vault(source, key)) as reopened:
        assert reopened.get("used_refresh", "31-4094")["subject"] == "family-31"
        assert reopened._usage().actual == 131072
    backup(source, snapshot, key_file)
    assert snapshot.stat().st_size <= MAX_BYTES
    recover(snapshot, output, key_file)
    with closing(Vault(output, key)) as restored:
        assert restored.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
    with closing(sqlite3.connect(source)) as unchanged:
        assert unchanged.execute("SELECT count(*) FROM records").fetchone()[0] == 131072


def test_full_legacy_database_can_rebuild_indexes_before_page_ceiling(tmp_path, monkeypatch):
    from test_vault_migration_memory import legacy_vault

    tmp_path.chmod(0o700)
    source, key = tmp_path / "legacy.db", Fernet.generate_key()
    legacy_vault(source, key, clients=2)
    with closing(sqlite3.connect(source)) as old:
        before = old.execute(
            "SELECT namespace,key,payload FROM records ORDER BY namespace,key"
        ).fetchall()
        assert old.execute("PRAGMA freelist_count").fetchone()[0] == 0
    # A legacy database above the new physical envelope remains readable. Its
    # mandatory new indexes need pages before SQLite clamps future growth.
    monkeypatch.setattr(Vault, "DATABASE_BYTES", 4096)
    with closing(Vault(source, key)) as upgraded:
        assert (
            upgraded.db.execute(
                "SELECT namespace,key,payload FROM records ORDER BY namespace,key"
            ).fetchall()
            == before
        )
        assert upgraded.get("grants", "synthetic-family") == {"client_id": "synthetic-client"}
        assert (
            upgraded.db.execute("PRAGMA max_page_count").fetchone()[0]
            == upgraded.db.execute("PRAGMA page_count").fetchone()[0]
        )
        with pytest.raises(PlayError, match="storage"):
            upgraded.put_many(
                [("clients", f"new-{i}", {"padding": "x" * 60000}, 600) for i in range(10)]
            )
        assert upgraded.get("access", "synthetic-access") == {"subject": "synthetic-family"}


def test_refresh_at_byte_full_family_preserves_old_tokens_and_neighbor(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from mcp.server.auth.provider import TokenError
    from test_hosted_grants import issue
    from test_oauth_capacity_isolation import rotate

    from pubship.hosted_config import HostedSettings
    from pubship.oauth import PlayOAuth

    tmp_path.chmod(0o700)
    key = Fernet.generate_key()
    settings = HostedSettings(
        "https://mcp.example.test",
        "synthetic",
        "synthetic",
        tmp_path / "vault.db",
        key,
        allowed_emails=frozenset({"owner@example.test"}),
    )
    with closing(Vault(settings.database, key)) as vault:
        provider = PlayOAuth(settings, vault)
        _, alice, _ = issue(provider, "alice", "a", legacy=True)
        _, bob, _ = issue(provider, "bob", "b", legacy=True)
        owner = Vault.digest("alice")
        for index in range(100):
            remaining = Vault.CONNECTION_BYTES - vault._usage().owner_bytes[owner]
            if remaining <= 512:
                break
            padding = min(60000, max(0, remaining * 3 // 4 - 500))
            vault.put(
                "used_refresh",
                f"padding-{index}",
                {"subject": "alice", "client_id": "a", "padding": "x" * padding},
                600,
            )
        else:
            pytest.fail("Failed to fill the real byte quota")
        before = vault.db.execute(
            "SELECT namespace,key,payload FROM records ORDER BY namespace,key"
        ).fetchall()
        with pytest.raises(TokenError) as rejected:
            rotate(provider, alice, "a")
        assert rejected.value.error == "invalid_request"
        assert (
            vault.db.execute(
                "SELECT namespace,key,payload FROM records ORDER BY namespace,key"
            ).fetchall()
            == before
        )
        assert asyncio.run(provider.load_access_token(alice.access_token)) is not None
        assert (
            asyncio.run(
                provider.load_refresh_token(SimpleNamespace(client_id="a"), alice.refresh_token)
            )
            is not None
        )
        fresh_bob = rotate(provider, bob, "b")
        assert fresh_bob.refresh_token != bob.refresh_token
        assert asyncio.run(provider.load_access_token(fresh_bob.access_token)) is not None
        assert asyncio.run(provider.load_access_token(alice.access_token)) is not None


def assert_accounting(vault):
    """Independent full-scan oracle for the transactional derived counters."""
    actual = vault.db.execute(
        "SELECT namespace,owner,records,payload_bytes,grants FROM record_usage "
        "ORDER BY namespace,owner"
    ).fetchall()
    expected = vault.db.execute(
        "SELECT r.namespace,COALESCE(o.owner,''),COUNT(*),SUM(length(r.payload)),"
        "SUM(CASE WHEN r.namespace='grants' AND r.key=o.owner THEN 1 ELSE 0 END) "
        "FROM records r LEFT JOIN record_owners o ON r.namespace=o.namespace AND r.key=o.key "
        "GROUP BY r.namespace,o.owner ORDER BY r.namespace,o.owner"
    ).fetchall()
    assert actual == expected


def test_usage_counters_cover_replacement_consume_expiry_and_disconnect(store):
    vault, _ = store
    for owner in ("alice", "bob"):
        grant(vault, owner)
        vault.put("access", owner, {"subject": owner, "padding": "x" * 1024}, 600)
    assert_accounting(vault)
    # REPLACE must debit the prior owner before FK cascade, then credit the new
    # owner and byte size. Application callers normally keep ownership stable.
    vault.put("access", "alice", {"subject": "bob"}, 600)
    vault.put("clients", "client", {"padding": "x" * 256}, 600)
    vault.put("clients", "client", {}, 600)
    assert_accounting(vault)
    vault.put("consent", "expired", {}, -1)
    vault.put("google_state", "next-write-prunes", {}, 600)
    assert vault.get("consent", "expired") is None
    assert_accounting(vault)
    assert vault.get("access", "alice", consume=True) == {"subject": "bob"}
    vault.delete("clients", "client")
    assert_accounting(vault)
    vault.delete_connection("alice")
    assert vault.get("grants", "bob") is not None
    assert_accounting(vault)
    vault.delete_connection("bob")
    assert_accounting(vault)
    assert vault._usage().reserved == 0
    assert vault._usage().reserved_bytes == 0


def test_usage_rollback_preserves_consumed_record_and_capacity(store, monkeypatch):
    vault, _ = store
    grant(vault, "alice")
    grant(vault, "bob")
    value = {"subject": "alice"}
    vault.put("refresh", "old", value, 600)
    before = vault._usage()
    with monkeypatch.context() as bounded:
        bounded.setattr(Vault, "CONNECTION_LIMIT", before.owners[Vault.digest("alice")])
        with pytest.raises(PlayError, match="capacity"):
            vault.replace_consumed(
                "refresh",
                "old",
                value,
                [
                    ("used_refresh", "old", value, 600),
                    ("refresh", "new", value, 600),
                    ("access", "new", value, 600),
                ],
            )
    assert vault.get("refresh", "old") == value
    assert vault.get("refresh", "new") is None
    assert vault._usage() == before
    assert_accounting(vault)
    vault.put("access", "bob", {"subject": "bob"}, 600)
    assert_accounting(vault)


def test_stale_or_tampered_usage_is_reconstructed_on_restart(store, tmp_path):
    vault, key = store
    grant(vault, "alice")
    vault.put("access", "token", {"subject": "alice"}, 600)
    vault.put("clients", "client", {}, 600)
    before = vault._usage()
    records = vault.db.execute("SELECT * FROM records ORDER BY namespace,key").fetchall()
    # A previous binary ignores these counters, and local corruption must not
    # become trusted accounting when the next supported process starts.
    vault.db.execute("UPDATE record_usage SET records=0,payload_bytes=0,grants=0")
    vault.db.execute("DROP TRIGGER usage_insert")
    vault.db.execute("INSERT INTO record_usage VALUES('access','fabricated',999999,999999,1)")
    vault.db.commit()
    vault.close()
    with closing(Vault(tmp_path / "vault.db", key)) as restored:
        assert restored._usage() == before
        assert (
            restored.db.execute("SELECT * FROM records ORDER BY namespace,key").fetchall()
            == records
        )
        assert_accounting(restored)
        restored.put("access", "next", {"subject": "alice"}, 600)
        assert_accounting(restored)


def test_usage_read_never_scans_lifecycle_rows(store):
    vault, _ = store
    grant(vault, "alice")
    vault.put_many([("used_refresh", str(i), {"subject": "alice"}, 600) for i in range(1000)])

    def deny_record_reads(action, table, _column, _database, _trigger):
        if action == sqlite3.SQLITE_READ and table in {"records", "record_owners"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    vault.db.set_authorizer(deny_record_reads)
    try:
        assert vault._usage().actual == 1001
    finally:
        vault.db.set_authorizer(None)
    assert_accounting(vault)
