"""Legacy migration streams large encrypted admission records and remains atomic."""

import json
import sqlite3
import time
import tracemalloc
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from pubship.errors import PlayError
from pubship.vault import Vault


def legacy_vault(path, key, *, clients=256, corrupt=False):
    cipher, expires = Fernet(key), time.time() + 600
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE records (namespace TEXT NOT NULL,key TEXT NOT NULL,"
            "expires REAL NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(namespace,key))"
        )

        def add(namespace, identity, value):
            digest = Vault.digest(identity)
            encoded = json.dumps(
                {"namespace": namespace, "key": digest, "expires": expires, "value": value}
            ).encode()
            assert len(encoded) <= 64 * 1024
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?)",
                (namespace, digest, expires, cipher.encrypt(encoded)),
            )

        add("grants", "synthetic-family", {"client_id": "synthetic-client"})
        add("access", "synthetic-access", {"subject": "synthetic-family"})
        for index in range(clients):
            add(
                "clients",
                str(index),
                {
                    "client_id": str(index),
                    "redirect_uris": ["https://client.example/callback"],
                    "client_name": "x" * 62000,
                },
            )
        if corrupt:
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?)",
                ("clients", Vault.digest("corrupt"), expires, b"invalid-ciphertext"),
            )
        payload_bytes = db.execute("SELECT SUM(length(payload)) FROM records").fetchone()[0]
    path.chmod(0o600)
    return payload_bytes


def test_migration_memory_does_not_scale_with_all_ciphertext(tmp_path):
    tmp_path.chmod(0o700)
    key, path = Fernet.generate_key(), tmp_path / "vault.db"
    payload_bytes = legacy_vault(path, key)
    assert payload_bytes > 20 * 1024**2
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    before = tracemalloc.get_traced_memory()[0]
    vault = None
    try:
        vault = Vault(path, key)
        peak = tracemalloc.get_traced_memory()[1] - before
        # A generous per-row working-set allowance. The old fetchall() needs
        # over 20 MiB for these valid records; the streamed scan stays below 1 MiB.
        assert peak < 4 * 1024**2, (
            f"Migration retained {peak} bytes for {payload_bytes} ciphertext bytes"
        )
        assert vault.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 258
        assert vault.db.execute("SELECT COUNT(*) FROM record_owners").fetchone()[0] == 2
        assert vault.get("clients", "255")["client_name"] == "x" * 62000
        assert vault.get("grants", "synthetic-family") == {"client_id": "synthetic-client"}
    finally:
        if not already_tracing:
            tracemalloc.stop()
        if vault is not None:
            vault.close()


def test_streamed_migration_failure_rolls_back_partial_owner_index(tmp_path):
    tmp_path.chmod(0o700)
    key, path = Fernet.generate_key(), tmp_path / "vault.db"
    legacy_vault(path, key, clients=3, corrupt=True)
    for _ in range(2):
        with pytest.raises(PlayError, match="verified"):
            Vault(path, key)
        with closing(sqlite3.connect(path)) as db, db:
            assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 6
            assert (
                db.execute("SELECT name FROM sqlite_master WHERE name='record_owners'").fetchone()
                is None
            )
