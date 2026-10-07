# Preview store listing text changes

`preview_store_listing_update` compares proposed copy against a localized listing
in an existing Publisher edit. It performs one GET with your own app permissions
and returns a proposed PATCH request. It does not send that PATCH.

## Example

Call the tool with:

```json
{
  "parameters": {
    "packageName": "com.example.app",
    "editId": "existing-edit-id",
    "language": "en-US"
  },
  "changes": {
    "shortDescription": "A concise description of the app."
  }
}
```

Configure the same app allowlist and Publisher credentials as
[existing-edit reads](publisher-edits.md). Obtain the edit ID separately; this tool
does not create an edit or replace one that has expired. A missing or inaccessible
listing fails rather than becoming an empty baseline.

## What the result means

- `preview_only: true` and `executed: false`: the proposal has not been applied.
- `data_scope: "edit_snapshot"`: the baseline can include unpublished changes.
- `fetched_at`: when the GET completed, not an expiry extension or freshness guarantee.
- `changes`: exact requested text, whether it differs, and whether the old field
  was present. An absent field is distinct from an empty string.
- `proposed_request`: the fixed `androidpublisher.edits.listings.patch` method,
  parameters and supplied text fields. There is no executable approval token.
- `provider_validation_performed: false` and `stale_check_performed: false`:
  Google has not validated the proposed change; someone may change or invalidate
  the edit after it is read. Re-read and review it before any future write.

Supply at least one of `title`, `shortDescription`, `fullDescription`. Unsupported
fields, unknown parameters, null/non-string values, invalid Unicode and requests
larger than 64 KiB are rejected. Text is preserved without trimming, normalizing
or interpreting HTML. An empty string is a requested empty value, not a promise
that Google will clear that field. Existing listing text is untrusted data.

Google documents 30/80/4000-character limits for the title, short and full
descriptions. This tool validates request shape and local bounds, not all of
Google's length, content or publishing rules. A successful preview is not approval,
publication, complete validation or concurrency protection. The response deliberately
omits a synthesized full listing so unknown and omitted fields are not invented.

## Coverage and verification

The composite tool reuses the implemented listing GET and remains read-only.
Optional [local staging](listing-staging.md) adds a separate prepare/apply workflow;
this preview response never authorizes it. Source coverage is 86/172 operations, including separately enabled edit lifecycle, track staging and [edit resource writes](edit-writes.md).
Tests use synthetic listings,
exercise actual MCP clients and prove that only a GET occurs. Live edit inspection
requires a caller-owned existing edit; no production edit is created for QA.

Sources: [Listing resource](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.listings),
[PATCH contract](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.listings/patch),
[edit lifecycle](https://developers.google.com/android-publisher/edits),
[store listing limits](https://support.google.com/googleplay/android-developer/answer/9859152?hl=en).
