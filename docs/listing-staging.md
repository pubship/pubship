# Prepare, review and stage listing text

This optional **local stdio** workflow stages `title`, `shortDescription` and
`fullDescription` in an existing Google Play edit. It never creates, replaces,
validates, commits or discards an edit. Staging is not publication. The original
[preview tool](listing-preview.md) remains read-only and returns no executable ID.

## Enable only the intended apps

Set both variables in your MCP client's local process environment:

```text
GOOGLE_PLAY_PACKAGES=com.example.app,com.example.second
GOOGLE_PLAY_WRITE_PACKAGES=com.example.app
```

The write allowlist is empty by default and must be a subset of the read
allowlist. Use the same `--env` option in the documented Codex/Claude Code commands,
or add it to your generic harness's `env` object, then restart the server.
The two staging tools are absent when writes are disabled. The hosted entry point
never registers them, regardless of machine environment or a customer's Google
Publisher scope. Hosted read consent does not authorize writing.

Google still checks the selected account's app access and store-presence editing
permission. An MCP allowlist cannot grant Google permissions. Keep distinct
accounts/trust boundaries in separate MCP processes.

## Prepare and review

Call `prepare_store_listing_update`:

```json
{
  "parameters": {
    "packageName": "com.example.app",
    "editId": "your-existing-edit-id",
    "language": "en-US"
  },
  "changes": {
    "shortDescription": "Your reviewed description."
  }
}
```

The server validates input and app scope, reads the edit's identity/expiry and
listing, and returns an exact diff plus an opaque `operation_id`. Review the target
app, edit, locale, old values and proposed text. Unknown fields, invalid Unicode,
null values and oversized inputs fail. Exact Unicode, whitespace and empty strings
are preserved. Google remains the authority on field and policy acceptance.

A preparation is usable only in that server process, for at most ten minutes and no
longer than the observed edit expiry. At most 100 preparations are retained; the
server rejects capacity overflow instead of silently discarding another operation.
Expired entries are removed on a later preparation; expiry is not a timed memory
erasure guarantee. Consumed entries are removed immediately. Restart loses all preparations. The original Google access token is retained only
in private process memory for this operation, so apply cannot silently switch to a
new ADC or gcloud identity. A token that expires or is revoked makes the operation
fail; it is not refreshed into a potentially different identity.

## Apply once

Call `apply_store_listing_update` with the returned ID repeated as confirmation:

```json
{
  "operation_id": "COPY_THE_RETURNED_ID",
  "confirmation": "COPY_THE_RETURNED_ID"
}
```

Confirmation identifies the exact prepared operation. It is explicit client intent,
not proof that a human reviewed it. Apply accepts no replacement target, baseline
or text. It consumes the preparation, rechecks edit metadata and the full listing,
and rejects observed drift before issuing one narrowly scoped PATCH. Missing and
empty values remain distinct. If the text already matches, no PATCH is necessary.

The result distinguishes a performed request from a no-op, returns actual provider
data on success, and keeps `committed: false` and `publication_verified: false`.
It does not authorize any later commit. A commit can include unrelated changes
already in the edit and requires the separately enabled [edit lifecycle](edit-lifecycle.md)
workflow and its own preparation. Listing staging itself never commits.

## Concurrency and recovery

The process serializes its staging operations and rejects reused IDs. Google does
not document an atomic listing compare-and-swap precondition: another client can
change the edit between the last GET and PATCH. The check detects observed drift,
not every race. Coordinate editing externally; avoid simultaneous Console/API
changes to the same edit.

After apply begins, its ID cannot be retried, including after a preflight failure.
Expired, inaccessible or invalidated edits are never replaced automatically. Read
and inspect the current state before making another preparation.

After a dispatched PATCH, a timeout, connection failure, malformed response or
server error may leave the outcome unknown. The server never retries the mutation
or claims no write occurred. Inspect the existing edit through read tools or Play
Console and reconcile its state before preparing anything again. No automatic
commit, rollback or discard is attempted. Even an HTTP success is not a promise
that Google accepted an entire edit or published a listing.

## Scope and evidence

This is text-only support for `edits.listings.patch`, not a generic mutation
executor. Video changes and full listing replacement remain outside this tool.
The catalog records the narrower implementation scope and local enablement rule.
Synthetic tests and real MCP protocol tests do not establish live Google write
acceptance. We do not create or change a production edit for QA.

Sources: [listing PATCH contract](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.listings/patch),
[edit lifecycle](https://developers.google.com/android-publisher/edits),
[commit behavior](https://developers.google.com/android-publisher/api-ref/rest/v3/edits/commit).
