"""Encrypted, expiring records for a single-instance hosted deployment.

Keys are outside SQLite. Record identity and expiry are inside the authenticated
ciphertext so copying a ciphertext to another tenant/key cannot rebind a grant.
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .errors import PlayError


@dataclass(frozen=True)
class VaultLimits:
    """Bounded single-worker enrollment; a family is one grant, not one user."""

    connections: int = 32

    def __post_init__(self):
        if type(self.connections) is not int or not 1 <= self.connections <= 32:
            raise PlayError("Vault connections must be an integer from 1 through 32.")

    @classmethod
    def from_env(cls):
        value = os.environ.get("PUBSHIP_SERVER_VAULT_CONNECTIONS", "32")
        if not value.isascii() or not value.isdecimal() or len(value) > 2:
            raise PlayError(
                "PUBSHIP_SERVER_VAULT_CONNECTIONS must be a decimal integer from 1 through 32."
            )
        return cls(connections=int(value))


@dataclass
class _Usage:
    counts: dict
    owners: dict
    actual: int
    reserved: int
    namespace_bytes: dict
    owner_bytes: dict
    actual_bytes: int
    reserved_bytes: int


def private_file(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            stat = os.fstat(file.fileno())
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                raise PlayError(
                    "Hosted secret files must be owned by the runtime user and have mode 0600 or 0400."
                )
            data = file.read(64 * 1024 + 1)
            if len(data) > 64 * 1024:
                raise PlayError("Hosted secret file is too large.")
            return data
    except OSError:
        raise PlayError("Cannot read the configured private hosted secret file.") from None


class Vault:
    # Unauthenticated registrations and incomplete browser flows cannot consume
    # the capacity reserved for existing customers' grants and token rotation.
    ADMISSION_LIMITS = {"clients": 10000, "consent": 5000, "google_state": 5000}
    ADMISSION_BYTES = {"clients": 8 * 1024**2, "consent": 8 * 1024**2, "google_state": 8 * 1024**2}
    MAX_CONNECTIONS = 32
    LIFECYCLE_LIMIT = MAX_CONNECTIONS * 4096
    # Reserve the entire bounded lifecycle allowance at connection admission.
    # A busy family cannot spend storage reserved for another admitted family.
    CONNECTION_LIMIT = 4096
    CONNECTION_BYTES = 4 * 1024**2
    DATABASE_BYTES = 384 * 1024**2

    def __init__(self, path: Path, key: bytes, *, limits: VaultLimits | None = None):
        self.limits = limits if limits is not None else VaultLimits()
        if not isinstance(self.limits, VaultLimits):
            raise PlayError("Vault limits are invalid.")
        try:
            self.cipher = Fernet(key)
        except (ValueError, TypeError):
            raise PlayError("Invalid vault encryption key.") from None
        parent = path.parent
        if (
            not parent.is_dir()
            or parent.is_symlink()
            or parent.stat().st_mode & 0o077
            or parent.stat().st_uid != os.getuid()
        ):
            raise PlayError(
                "Vault directory must be private (0700), owned by the runtime user and not a symlink."
            )
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            stat = os.fstat(fd)
            os.close(fd)
            if stat.st_mode & 0o077 or stat.st_uid != os.getuid():
                raise PlayError("Vault file must be private and owned by the runtime user.")
            self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA recursive_triggers=ON")
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS records (namespace TEXT NOT NULL, key TEXT NOT NULL, expires REAL NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(namespace,key))"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS record_expiry_lookup ON records(expires)")
            # Derived index, rebuilt atomically from authenticated payloads on
            # every startup. Keep the original records schema rollback-compatible;
            # an older binary may write records without maintaining this index.
            # Counters are derived too: never trust a previous process, an older
            # binary or stale/tampered accounting when reconstructing ownership.
            for trigger in ("usage_insert", "usage_delete", "usage_owner", "usage_payload"):
                self.db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self.db.execute("DROP TABLE IF EXISTS record_usage")
            self.db.execute("DROP TABLE IF EXISTS record_owners")
            self.db.execute(
                "CREATE TABLE record_owners (namespace TEXT NOT NULL, key TEXT NOT NULL, owner TEXT NOT NULL, "
                "PRIMARY KEY(namespace,key), FOREIGN KEY(namespace,key) REFERENCES records(namespace,key) ON DELETE CASCADE)"
            )
            self.db.execute("CREATE INDEX record_owner_lookup ON record_owners(owner)")
            self.db.execute("DELETE FROM records WHERE expires <= ?", (time.time(),))
            # Records stay unchanged during this scan. Stream ciphertext rows
            # while building the separate index; loading all payloads at once
            # could exceed runtime memory for an otherwise valid bounded vault.
            for namespace, digest, payload in self.db.execute(
                "SELECT namespace, key, payload FROM records"
            ):
                value = self._decode(namespace, digest, payload, strict_identity=True)
                owner = self._owner(namespace, digest, value)
                if owner:
                    self.db.execute(
                        "INSERT INTO record_owners VALUES (?,?,?)", (namespace, digest, owner)
                    )
            self._create_usage()
            self.db.commit()
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            # Apply the runtime ceiling AFTER rebuilding derived indexes. A
            # full legacy database may need extra index pages to start safely.
            # SQLite preserves a legacy database larger than the configured
            # ceiling by using its current page count; no records are removed.
            self.db.execute(f"PRAGMA max_page_count={self.DATABASE_BYTES // page_size}")
        except (OSError, sqlite3.Error, PlayError) as error:
            if hasattr(self, "db"):
                self.db.rollback()
                self.db.close()
            if isinstance(error, PlayError):
                raise
            raise PlayError("Cannot open the private credential vault.") from None
        self.lock = threading.RLock()

    @staticmethod
    def digest(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def put(self, namespace, key, value, ttl):
        self.put_many([(namespace, key, value, ttl)])

    def put_many(self, records):
        """Atomically store related records, including their capacity checks.

        Each entry is (namespace, key, value, ttl). Callers issuing a grant/code
        or access/refresh pair should submit the entire set together. This does
        not include a preceding get(consume=True) in the transaction.
        """
        self._put_many(records)

    def replace_consumed(self, namespace, key, expected, records):
        """Compare, consume and replace one credential in a single transaction.

        Capacity or persistence failure rolls back consumption, replay marker
        and replacement credentials together. A concurrent consumer loses.
        """
        return self._put_many(records, consumed=(namespace, key, expected))

    @classmethod
    def _owner(cls, namespace, digest, value):
        if not isinstance(value, dict):
            return None
        if namespace == "grants" and isinstance(value.get("client_id"), str):
            return digest
        if namespace in {"codes", "access", "refresh", "used_refresh"}:
            subject = value.get("subject")
            if isinstance(subject, str) and subject:
                return cls.digest(subject)
        return None

    def _create_usage(self):
        """Build counters once, then maintain them in the record transaction.

        The original records schema is unchanged. BEFORE DELETE sees ownership
        before its foreign-key cascade. recursive_triggers makes REPLACE follow
        that path too. A new owned record moves from the unowned bucket when its
        derived owner is inserted. Expiry, consume and disconnect therefore use
        the same accounting, and transaction rollback restores both together.
        """
        self.db.execute(
            "CREATE TABLE record_usage (namespace TEXT NOT NULL, owner TEXT NOT NULL, "
            "records INTEGER NOT NULL, payload_bytes INTEGER NOT NULL, grants INTEGER NOT NULL, "
            "PRIMARY KEY(namespace,owner))"
        )
        self.db.execute(
            "INSERT INTO record_usage "
            "SELECT r.namespace, COALESCE(o.owner,''), COUNT(*), SUM(length(r.payload)), "
            "SUM(CASE WHEN r.namespace='grants' AND r.key=o.owner THEN 1 ELSE 0 END) "
            "FROM records r LEFT JOIN record_owners o ON r.namespace=o.namespace AND r.key=o.key "
            "GROUP BY r.namespace,o.owner"
        )
        self.db.execute(
            "CREATE TRIGGER usage_insert AFTER INSERT ON records BEGIN "
            "INSERT INTO record_usage VALUES(NEW.namespace,'',1,length(NEW.payload),0) "
            "ON CONFLICT(namespace,owner) DO UPDATE SET records=records+1, "
            "payload_bytes=payload_bytes+length(NEW.payload); END"
        )
        owner = (
            "COALESCE((SELECT owner FROM record_owners "
            "WHERE namespace=OLD.namespace AND key=OLD.key),'')"
        )
        self.db.execute(
            "CREATE TRIGGER usage_delete BEFORE DELETE ON records BEGIN "
            "UPDATE record_usage SET records=records-1, payload_bytes=payload_bytes-length(OLD.payload), "
            "grants=grants-CASE WHEN OLD.namespace='grants' AND OLD.key=owner THEN 1 ELSE 0 END "
            f"WHERE namespace=OLD.namespace AND owner={owner}; "
            "DELETE FROM record_usage WHERE namespace=OLD.namespace AND records=0; END"
        )
        payload_size = (
            "(SELECT length(payload) FROM records WHERE namespace=NEW.namespace AND key=NEW.key)"
        )
        self.db.execute(
            "CREATE TRIGGER usage_owner AFTER INSERT ON record_owners BEGIN "
            "UPDATE record_usage SET records=records-1, "
            f"payload_bytes=payload_bytes-{payload_size} "
            "WHERE namespace=NEW.namespace AND owner=''; "
            "DELETE FROM record_usage WHERE namespace=NEW.namespace AND owner='' AND records=0; "
            f"INSERT INTO record_usage VALUES(NEW.namespace,NEW.owner,1,{payload_size}, "
            "CASE WHEN NEW.namespace='grants' AND NEW.key=NEW.owner THEN 1 ELSE 0 END) "
            "ON CONFLICT(namespace,owner) DO UPDATE SET records=records+1, "
            "payload_bytes=payload_bytes+excluded.payload_bytes, grants=grants+excluded.grants; END"
        )
        self.db.execute(
            "CREATE TRIGGER usage_payload AFTER UPDATE OF payload ON records BEGIN "
            "UPDATE record_usage SET payload_bytes=payload_bytes+length(NEW.payload)-length(OLD.payload) "
            f"WHERE namespace=OLD.namespace AND owner={owner}; END"
        )

    def _usage(self):
        # At normal occupancy this reads a few rows per family/namespace, not
        # every lifecycle token. All record mutations update these counters in
        # the same SQLite transaction; startup reconstructs them from scratch.
        rows = self.db.execute(
            "SELECT namespace, owner, records, payload_bytes, grants FROM record_usage"
        )
        counts, owners, namespace_bytes, owner_bytes, live = {}, {}, {}, {}, set()
        for namespace, owner, count, size, has_grant in rows:
            counts[namespace] = counts.get(namespace, 0) + count
            namespace_bytes[namespace] = namespace_bytes.get(namespace, 0) + size
            if namespace not in self.ADMISSION_LIMITS and owner:
                owners[owner] = owners.get(owner, 0) + count
                owner_bytes[owner] = owner_bytes.get(owner, 0) + size
                if has_grant:
                    live.add(owner)
        actual = sum(n for ns, n in counts.items() if ns not in self.ADMISSION_LIMITS)
        actual_bytes = sum(
            n for ns, n in namespace_bytes.items() if ns not in self.ADMISSION_LIMITS
        )
        reserved = actual + sum(max(0, self.CONNECTION_LIMIT - owners[o]) for o in live)
        reserved_bytes = actual_bytes + sum(
            max(0, self.CONNECTION_BYTES - owner_bytes[o]) for o in live
        )
        return _Usage(
            counts,
            owners,
            actual,
            reserved,
            namespace_bytes,
            owner_bytes,
            actual_bytes,
            reserved_bytes,
        )

    def _put_many(self, records, consumed=None):
        prepared = {}
        now = time.time()
        for namespace, key, value, ttl in records:
            expires = now + ttl
            digest = self.digest(key)
            encoded = json.dumps(
                {"namespace": namespace, "key": digest, "expires": expires, "value": value},
                allow_nan=False,
            ).encode()
            if len(encoded) > 64 * 1024:
                raise PlayError("Authorization record is too large.")
            prepared[namespace, digest] = (
                namespace,
                digest,
                expires,
                self.cipher.encrypt(encoded),
                self._owner(namespace, digest, value),
            )
        try:
            with self.lock, self.db:
                self.db.execute("DELETE FROM records WHERE expires <= ?", (now,))
                before = self._usage()
                if consumed is not None:
                    namespace, key, expected = consumed
                    digest = self.digest(key)
                    row = self.db.execute(
                        "SELECT payload FROM records WHERE namespace=? AND key=?",
                        (namespace, digest),
                    ).fetchone()
                    if row is None or self._decode(namespace, digest, row[0]) != expected:
                        return False
                    self.db.execute(
                        "DELETE FROM records WHERE namespace=? AND key=?", (namespace, digest)
                    )
                self.db.executemany(
                    "INSERT OR REPLACE INTO records (namespace,key,expires,payload) VALUES (?,?,?,?)",
                    [record[:4] for record in prepared.values()],
                )
                self.db.executemany(
                    "INSERT INTO record_owners VALUES (?,?,?)",
                    [
                        (record[0], record[1], record[4])
                        for record in prepared.values()
                        if record[4]
                    ],
                )
                after = self._usage()
                for namespace, limit in self.ADMISSION_LIMITS.items():
                    if after.counts.get(namespace, 0) > max(limit, before.counts.get(namespace, 0)):
                        raise PlayError("Hosted authorization capacity reached; try again later.")
                    if after.namespace_bytes.get(namespace, 0) > max(
                        self.ADMISSION_BYTES[namespace], before.namespace_bytes.get(namespace, 0)
                    ):
                        raise PlayError("Hosted authorization capacity reached; try again later.")
                record_limit = (
                    self.LIFECYCLE_LIMIT * self.limits.connections // self.MAX_CONNECTIONS
                )
                byte_limit = self.CONNECTION_BYTES * self.limits.connections
                if (
                    after.actual > max(record_limit, before.actual)
                    or after.reserved > max(record_limit, before.reserved)
                    or after.actual_bytes > max(byte_limit, before.actual_bytes)
                    or after.reserved_bytes > max(byte_limit, before.reserved_bytes)
                ):
                    raise PlayError("Hosted authorization capacity reached; try again later.")
                for owner, count in after.owners.items():
                    if count > max(
                        self.CONNECTION_LIMIT, before.owners.get(owner, 0)
                    ) or after.owner_bytes[owner] > max(
                        self.CONNECTION_BYTES, before.owner_bytes.get(owner, 0)
                    ):
                        raise PlayError(
                            "Connection authorization capacity reached; reconnect this account."
                        )
                return True
        except sqlite3.Error:
            raise PlayError("Credential storage is unavailable; reconnect later.") from None

    def get(self, namespace, key, *, consume=False):
        digest = self.digest(key)
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE" if consume else "BEGIN")
                row = self.db.execute(
                    "SELECT payload FROM records WHERE namespace=? AND key=?", (namespace, digest)
                ).fetchone()
                if consume:
                    self.db.execute(
                        "DELETE FROM records WHERE namespace=? AND key=?", (namespace, digest)
                    )
                self.db.commit()
            except sqlite3.Error:
                self.db.rollback()
                raise PlayError("Credential storage is unavailable; reconnect later.") from None
        if row is None:
            return None
        return self._decode(namespace, digest, row[0])

    def _decode(self, namespace, digest, payload, *, strict_identity=False):
        try:
            record = json.loads(self.cipher.decrypt(payload))
            if record["namespace"] != namespace or record["key"] != digest:
                if strict_identity:
                    raise PlayError(
                        "Credential record could not be verified; reconnect the account."
                    )
                return None
            if record["expires"] <= time.time():
                return None
            return record["value"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise PlayError(
                "Credential record could not be verified; reconnect the account."
            ) from None

    def delete(self, namespace, key):
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM records WHERE namespace=? AND key=?", (namespace, self.digest(key))
            )

    def delete_connection(self, subject):
        """Revoke a grant and reclaim its token family, without other tenants."""
        owner = self.digest(subject)
        try:
            with self.lock, self.db:
                self.db.execute(
                    "DELETE FROM records WHERE (namespace,key) IN (SELECT namespace,key FROM record_owners WHERE owner=?) OR (namespace='grants' AND key=?)",
                    (owner, owner),
                )
        except sqlite3.Error:
            raise PlayError("Credential storage is unavailable; reconnect later.") from None

    def close(self):
        with self.lock:
            self.db.close()
