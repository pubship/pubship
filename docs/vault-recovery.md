# Private vault backup and reconnect-only recovery

Optional operator tooling for self-hosted instances. The project does not operate backups or a Google-connected service for you.

## Recovery decision

An old SQLite snapshot can contain grants that were disconnected after the backup.
It cannot prove subsequent revocation history. **Never point the hosted service at
a backup or copy old credential records into an active vault.** This tool validates
the snapshot and matching key, then creates a **new empty vault**. Every client
must register and connect again through Google consent. There is no option to
preserve credentials.

Recovery copies none of the old grants, access tokens, refresh tokens, used-refresh
markers, authorization codes, client registrations, consent flows or Google states.
This also excludes unknown/future record namespaces. An expired or disconnected
credential cannot become active through this recovery procedure. Freshly authorized
connections can persist and refresh after a subsequent normal service restart.

## Create a backup

Run as the vault's runtime UID (10001 in the hosted image), using the accepted
package/runtime Python. Source, target and key file must each have a private
`0700` containing directory owned by that UID. Files must be regular, singly
linked, owned by the UID, with mode `0600` or `0400`; output mode is `0600`.
Paths must not contain symlinks. On macOS, use the real `/private/...` temporary
path instead of a `/var/...` alias. Create the target directory beforehand.

Illustrative paths below are placeholders, outside repositories and build contexts:

```sh
python -m pubship.vault_recovery backup \
  --source /var/lib/pubship/vault.db \
  --target /private-backups/vault-snapshot.db \
  --key-file /run/secrets/vault-key
```

In a source checkout, prefix the command with `uv run --frozen --all-extras`.
An existing target or target SQLite sidecar is always rejected. Use a unique target
name for each run. The source is opened read-only; the online backup includes
committed WAL data and does not alter source credential records. SQLite can access
its shared-memory coordination file during a live backup. Do not use immutable
mode or a simple file copy for a live source.

The snapshot preserves encrypted record payloads. It is **not whole-file
encryption**: SQLite structure, namespaces, hashed keys and expiry metadata remain
visible. Each record is authenticated, including expired records; an authenticated
format marker binds the backup to the key even when it contains no records. For
an empty source there is no existing ciphertext to prove the historical key; the
backup establishes the provided key as that snapshot's key. The marker does not
prove snapshot completeness or freshness. Retain a separately protected checksum
and operational inventory if those properties are required.

Use an encrypted, access-controlled off-server destination and an independently
protected copy of the matching key. Do not place the plaintext key beside the snapshot in
the same backup export. A separately encrypted key escrow may accompany a whole-file
encrypted snapshot only when its decryption identity is held separately from
both servers, using the sample operator units in scripts/ops. Keep transfers, tool results, logs, client exports and
process-only preparations out of the vault backup. Transfer and retention policy
are operator responsibilities, not actions performed by this tool.

## Validate and recover in isolation

Retrieve a snapshot and its separately protected matching key over an approved
encrypted channel into private directories owned by the recovery UID. The
snapshot must be self-contained and have no WAL, journal or shared-memory sidecar.
This tool accepts snapshots created by its `backup` operation, not arbitrary raw
SQLite copies.

```sh
python -m pubship.vault_recovery recover \
  --source /private-backups/vault-snapshot.db \
  --target /private-recovery/new-vault.db \
  --key-file /private-recovery-secrets/vault-key
```

A successful command prints only a completion message. Failure returns nonzero
with a generic diagnostic, does not publish the target, and normally removes its
private temporary files. Wrong keys, corrupted records, invalid database structure,
unsafe ownership/modes, symlinks and existing targets fail closed. The recovery
source is opened read-only and immutable after rejecting sidecars; validation does
not write to it. Never modify the input while verification runs.

Test a recovery with isolated synthetic state before any approved production
cutover. Stop the single hosted process before changing its vault path/mount;
retain the previous private vault/key under the chosen retention policy. Configure
the empty output and matching key, then start one process and require every user
to reconnect. Keep public access closed until the wider acceptance criteria pass.
The command does not stop services, change mounts, overwrite an existing database,
transfer files or revoke Google-side consent.

## Bounds and failure limits

The tool limits each database/sidecar to 512 MiB, validates at most 151,072 records,
and decrypts one record at a time (64 KiB plaintext limit). SQLite online backup
advances 64 pages per step with a cooperative 30-second deadline, a one-second
SQLite busy timeout and deadline checks during SQL/record validation. Oversized
or continuously changing sources can fail and should be retried during an approved
quiet period. Kernel filesystem operations, including fsync, are not forcibly
interrupted by that deadline. These are limits, not capacity measurements.

Temporary output is private and created in the target directory. After validation,
the closed file is synced and published using an atomic hard link that cannot
replace an existing target; the directory is synced as well. The filesystem must
support these operations. Ordinary failures clean up temporary output; process
termination, power loss or a filesystem that refuses cleanup can leave a private
`.vault-recovery-*` file or sidecar. Do not run it as a substitute for encrypted
storage or protection against a privileged/same-UID process changing files during
the operation. Deletion does not guarantee secure erasure.

## Synthetic verification

```sh
uv run --frozen --all-extras pytest tests/test_vault_recovery.py
uv run --frozen --all-extras ruff check src/pubship/vault_recovery.py tests/test_vault_recovery.py
uv run --frozen --all-extras ruff format --check src/pubship/vault_recovery.py tests/test_vault_recovery.py
```

Tests cover live WAL capture, source preservation, wrong keys (including empty
snapshots), corruption including expired records, path/ownership/mode rejection,
resource limits and failed publication. The HTTP fixture disconnects a synthetic
connection after backup, recovers, rejects old access/refresh/code/consent state,
then creates a fresh synthetic connection and verifies persistence and token
rotation after restart. Google token exchange is mocked; this is not a real
Google consent, deployed-container or off-server transfer receipt.

Reference behavior: [Python SQLite backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup),
[SQLite online backup API](https://www.sqlite.org/c3ref/backup_finish.html),
and [SQLite immutable URI mode](https://www.sqlite.org/uri.html).
