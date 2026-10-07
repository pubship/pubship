# Hosted file transfers

**Published and controlled-deployed 0.20.0.** File-transfer implementation and controlled-runtime acceptance have separate [verification receipts](validation.md#accepted-020-release-deployment-and-website-2026-10-06). Bundle #56 tracks combined acceptance, publication and deployment separately.

## Use one connection

A transfer belongs to one Google connection and MCP client. The MCP tools and byte requests must use that same connection's OAuth credentials. A different connection to the same Google account does not inherit its handles. Never paste access tokens into tool arguments, put them in URLs or copy another client's token store.

1. Call `begin_upload_transfer` with a fixed supported method, exact parameters, MIME type, positive `size_bytes` and lowercase SHA-256. Include explicit policy-document metadata in `body` when needed.
2. Stream a single `PUT` to the returned same-origin `content_path`, using the connection's bearer authorization. The MIME type, length and hash must match the reservation. Query parameters, compression, ranges, append and resumable uploads are unsupported.
3. Inspect `get_transfer_status` if the HTTP response was lost. A READY handle can be claimed once by the corresponding prepare tool; a consumed or interrupted upload requires a new reservation.
4. Review the prepare result, then repeat its operation ID to the matching apply tool. Google writes are single-attempt. Do not automatically retry an uncertain result.
5. Use `discard_transfer` when a file is no longer needed. This cannot undo a Google mutation or erase a copy already received by a client.

`download_artifact` accepts only fixed generated/system APK identifiers. It reserves bounded storage, receives the complete Google response and returns a handle. An authenticated `GET` on its content route delivers the bytes. Repeating this delivery within the handle's lifetime does not repeat the Google download. The service does not unpack, execute or claim to inspect APK signatures.

## Authorization and lifetime

Handles bind the owner, exact method, canonical target and original authorization deadline. Every byte route checks the current bearer record, resource, connection/client, capability and app or store/app dimension before consuming an upload body. Checks continue between bounded chunks. Refresh does not extend an existing transfer or preparation.

The lifetime is the shortest of the configured transfer TTL, original authorization lifetime and applicable preparation/edit deadline. It never exceeds ten minutes. Disconnect invalidates transfers and preparations; an already dispatched provider request may finish before disconnect returns and cannot be undone. Already delivered bytes cannot be recalled.

App-store catalog and management permissions use exact store/app pairs. Event feeds require explicit store-wide access. These are independent of ordinary app-read packages. Policy-document uploads preserve their explicit metadata and multipart bytes. Internal-sharing links and catalog delivery tokens are sent only to the authorized client and may remain in its transcripts.

## Operator configuration

Set `PUBSHIP_SERVER_TRANSFER_DIRECTORY` to an absolute private directory. This is a required 0.20.0 operator migration: startup fails if it is absent, including an otherwise valid 0.19.0 environment. It must be owned by the service user, mode `0700`, with no symlink components. The supported hosted backend is Linux with `O_TMPFILE` and physical file-allocation support; a sparse truncate or fallback to shared temporary storage is not sufficient. Use a dedicated volume and mount it at `/var/lib/pubship-transfers` for the non-root container user, UID/GID 10001. Keep it separate from the encrypted OAuth vault and its backups.

The service keeps metadata in process memory and bytes in unlinked temporary files. No recoverable transfer catalog is stored in the vault. Process restart loses handles and releases open files. Deletion is not a secure-erasure guarantee. Physical reservation failure, exhausted headroom and unavailable storage fail closed.

Every setting below is prefixed with `PUBSHIP_SERVER_TRANSFER_`:

| Setting | Default | Meaning |
| --- | ---: | --- |
| `OBJECT_BYTES` | 1 GiB | Maximum single transfer before the method's own limit |
| `CONNECTION_BYTES` | 2 GiB | All charged files for one connection/client |
| `TOTAL_BYTES` | 8 GiB | All charged files in this process |
| `CONNECTION_HANDLES` | 4 | Reserved, receiving, ready, claimed and cleanup-pending files per owner |
| `TOTAL_HANDLES` | 32 | Total charged file records |
| `CONNECTION_STREAMS` | 1 | Concurrent byte streams per owner |
| `TOTAL_STREAMS` | 4 | Concurrent byte streams in this process |
| `TTL_SECONDS` | 600 | Maximum handle lifetime; cannot exceed 600 |
| `FREE_DISK_BYTES` | 1 GiB | Disk headroom floor; operators can require more |
| `IDLE_TIMEOUT_SECONDS` | 30 | Maximum idle byte-delivery wait |
| `STREAM_TIMEOUT_SECONDS` | 600 | Overall byte-request limit, also bounded by the original deadline |

Environment values are positive decimal integers in bytes or seconds as named. Validate defaults against the actual volume capacity. The 1 GiB object default is configurable; it is not a permanent protocol limit. Method-specific contract ceilings, available physical space and shared quotas can impose a smaller effective limit. The service reports sanitized limit errors without exposing another tenant's usage or filesystem paths.

All receiving, ready, claimed and pending-cleanup allocations remain charged. A busy file cannot be closed underneath its active chunk; invalidation prevents further delivery. Failed cleanup retains its reservation for bounded retries. These controls are for one process/worker and do not establish a distributed quota system.

JSON MCP/OAuth requests retain their 256 KiB limit. Hosted observation and preparation metadata limits remain independent. External APK declarations use JSON and are never fetched; their hosted body therefore remains bounded by the ordinary request limit.

## Verification and client limitations

Synthetic tests can verify provider routes, exact bytes, ownership, failures and cleanup without making production writes. They do not establish real Google program eligibility, maximum upload acceptance or every desktop client's file UX. Use the reference client's tested same-connection path; do not assume a Claude Code or generic desktop attachment creates a hosted transfer automatically. The source checkout includes a reference client using one in-memory OAuth connection:

```bash
uv run --frozen --all-extras python scripts/hosted_transfer_client.py --server https://selfhost.example.test --scope play:edit-artifacts --scope play:artifact-downloads
```

The controlled endpoint runs accepted 0.22.0; public enrollment remains closed. For your own self-hosted instance, replace the origin with its HTTPS service. The browser asks for exact app consent. The helper keeps MCP credentials only in its process and receives its OAuth callback on `127.0.0.1:8765` by default; `--callback-port` selects another local port.

At its prompt, enter one JSON command per line. For example, after choosing your own app and existing edit:

```json
{"command":"upload","source":"/private/app.apk","method":"androidpublisher.edits.apks.upload","parameters":{"packageName":"com.example.app","editId":"YOUR_EDIT_ID"},"mime_type":"application/vnd.android.package-archive"}
```

This reserves and sends bytes to the MCP service; it does not upload them to Google. The helper prints selected intent, hashes and outcome metadata, not complete provider payloads, installation links or delivery tokens. Use an appropriately authorized MCP client when those results are needed. Use the returned handle with `prepare_artifact_upload` through an explicit `tool` command and review the intent, effects and hashes. An explicit matching apply command can perform the Google mutation. Nothing applies automatically.

For a completed download handle, use `{"command":"download","transfer_id":"YOUR_HANDLE","destination":"/private/new.apk"}`. The destination directory must already exist, be owned by you and have mode `0700`; the helper exclusively creates a new `0600` file, verifies length/hash and preserves existing files. Local source/destination paths stay on your machine. `{"command":"quit"}` ends the session; credentials are not persisted. A lost upload response requires a status check, not a blind retry.

Independent tests exercised the actual SDK OAuth provider and session over local HTTPS, including token refresh and both byte directions. They used synthetic Google responses and do not establish live program eligibility or desktop-client attachment behavior.

## Bounded load acceptance

Synthetic load fixtures use small files and do not establish maximum-size transfer throughput or production proxy behavior. Native client attachments are not transfer handles; use the reference same-connection workflow above.