"""Atomic refresh, connection capacity and old-vault migration regressions."""

import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import TokenError
from test_hosted_grants import issue
from test_hosted_grants import provider as provider

from pubship.errors import PlayError
from pubship.hosted_grants import READ_SCOPE
from pubship.oauth import PlayOAuth
from pubship.vault import Vault


def rotate(provider, tokens, client):
    async def run():
        identity = SimpleNamespace(client_id=client)
        old = await provider.load_refresh_token(identity, tokens.refresh_token)
        assert old is not None
        return await provider.exchange_refresh_token(identity, old, [READ_SCOPE])

    return asyncio.run(run())


def test_busy_connection_cannot_revoke_or_use_other_reserved_capacity(provider, monkeypatch):
    monkeypatch.setattr(Vault, "LIFECYCLE_LIMIT", 12)
    monkeypatch.setattr(Vault, "CONNECTION_LIMIT", 6)
    _, alice, _ = issue(provider, "alice", "a", legacy=True)
    _, bob, _ = issue(provider, "bob", "b", legacy=True)
    first = alice
    alice = rotate(provider, alice, "a")
    before = provider.vault.db.execute(
        "SELECT namespace,key,payload FROM records ORDER BY namespace,key"
    ).fetchall()
    with pytest.raises(TokenError) as failure:
        rotate(provider, alice, "a")
    assert failure.value.error == "invalid_request"
    assert (
        provider.vault.db.execute(
            "SELECT namespace,key,payload FROM records ORDER BY namespace,key"
        ).fetchall()
        == before
    )
    assert asyncio.run(provider.load_access_token(alice.access_token))
    bob = rotate(provider, bob, "b")
    assert asyncio.run(provider.load_access_token(bob.access_token))
    # Replay still revokes only the issuing family and releases its reservation.
    assert (
        asyncio.run(
            provider.load_refresh_token(SimpleNamespace(client_id="a"), first.refresh_token)
        )
        is None
    )
    assert asyncio.run(provider.load_access_token(alice.access_token)) is None
    assert asyncio.run(provider.load_access_token(bob.access_token))
    assert not provider.vault.db.execute(
        "SELECT 1 FROM record_owners WHERE owner=?", (Vault.digest("alice"),)
    ).fetchone()
    issue(provider, "replacement", "c", legacy=True)


def test_admission_reserves_whole_family_and_disconnect_reclaims_it(provider, monkeypatch):
    monkeypatch.setattr(Vault, "LIFECYCLE_LIMIT", 8)
    monkeypatch.setattr(Vault, "CONNECTION_LIMIT", 4)
    issue(provider, "a", "a", legacy=True)
    issue(provider, "b", "b", legacy=True)
    with pytest.raises(PlayError, match="capacity"):
        issue(provider, "c", "c", legacy=True)
    assert provider.vault.get("grants", "c") is None
    provider.disconnect("a")
    issue(provider, "c", "c", legacy=True)


def test_atomic_compare_consume_has_one_winner(provider):
    provider.vault.put("refresh", "old", {"subject": "one"}, 600)

    def replace(index):
        return provider.vault.replace_consumed(
            "refresh", "old", {"subject": "one"}, [("refresh", str(index), {"subject": "one"}, 600)]
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(replace, range(8))) == 1


def test_old_encrypted_vault_migrates_ownership_without_changing_authority(provider):
    _, tokens, principal = issue(provider, legacy=True)
    path = provider.settings.database
    key = provider.settings.encryption_key
    original = provider.vault.get("grants", "one")
    provider.vault.close()
    # Rebuild the actual pre-fix five-column schema with identical ciphertext.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("ALTER TABLE records RENAME TO old_records")
        db.execute(
            "CREATE TABLE records (namespace TEXT NOT NULL,key TEXT NOT NULL,expires REAL NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(namespace,key))"
        )
        db.execute("INSERT INTO records SELECT namespace,key,expires,payload FROM old_records")
        db.execute("DROP TABLE old_records")
    provider.vault = Vault(path, key)
    restarted = PlayOAuth(provider.settings, provider.vault)
    assert restarted.vault.get("grants", "one") == original
    assert restarted.authorize_current(principal).scopes == frozenset({READ_SCOPE})
    fresh = rotate(restarted, tokens, "client-one")
    assert asyncio.run(restarted.load_access_token(fresh.access_token))
    restarted.disconnect("one")
    assert restarted.vault.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    restarted.vault.close()


def test_migration_rejects_rebound_ciphertext(tmp_path):
    tmp_path.chmod(0o700)
    key = Fernet.generate_key()
    path = tmp_path / "vault.db"
    expires = time.time() + 600
    payload = Fernet(key).encrypt(
        json.dumps(
            {
                "namespace": "grants",
                "key": Vault.digest("alice"),
                "expires": expires,
                "value": {"client_id": "a"},
            }
        ).encode()
    )
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE records (namespace TEXT NOT NULL,key TEXT NOT NULL,expires REAL NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(namespace,key))"
        )
        db.execute(
            "INSERT INTO records VALUES (?,?,?,?)",
            ("grants", Vault.digest("bob"), expires, payload),
        )
    path.chmod(0o600)
    with pytest.raises(PlayError, match="verified"):
        Vault(path, key)


def test_bearer_authentication_does_not_queue_behind_mutation(provider):
    import threading

    _, tokens, principal = issue(provider, legacy=True)
    locked, release = threading.Event(), threading.Event()

    def busy():
        with provider.grant_lock(principal.subject):
            locked.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(busy)
        assert locked.wait(2)
        try:
            auth = pool.submit(lambda: asyncio.run(provider.load_access_token(tokens.access_token)))
            assert auth.result(timeout=2) == principal
        finally:
            release.set()
        running.result(timeout=2)
    provider.disconnect(principal.subject)
    assert asyncio.run(provider.load_access_token(tokens.access_token)) is None


def test_failed_owner_migration_is_atomic_and_original_schema_remains(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "vault.db"
    key = Fernet.generate_key()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE records (namespace TEXT NOT NULL,key TEXT NOT NULL,expires REAL NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(namespace,key))"
        )
        db.execute(
            "INSERT INTO records VALUES (?,?,?,?)",
            ("grants", Vault.digest("broken"), time.time() + 600, b"invalid-ciphertext"),
        )
    path.chmod(0o600)
    for _ in range(2):
        with pytest.raises(PlayError, match="verified"):
            Vault(path, key)
        with closing(sqlite3.connect(path)) as db, db:
            assert [row[1] for row in db.execute("PRAGMA table_info(records)")] == [
                "namespace",
                "key",
                "expires",
                "payload",
            ]
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
            assert (
                db.execute("SELECT name FROM sqlite_master WHERE name='record_owners'").fetchone()
                is None
            )
