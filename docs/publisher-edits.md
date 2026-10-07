# Inspect an existing Publisher edit

`read_publisher_edit` reads an edit you already have. It does not open, replace,
validate, commit or delete edits. Obtain the edit ID from your existing publishing
workflow; the MCP does not discover one or create one on your behalf.

Google creates an edit as a snapshot of app state. It can subsequently contain
unpublished changes. A Console change or another committed edit can invalidate
it, and creating another edit can invalidate your current one. A successful read
is evidence about that edit, not evidence that its contents are live.
[Google edit lifecycle](https://developers.google.com/android-publisher/edits).

## Example

First inspect the contract:

```json
{"method": "androidpublisher.edits.listings.get"}
```

Pass that object to `describe_api_method`, then call `read_publisher_edit`:

```json
{
  "method": "androidpublisher.edits.listings.get",
  "parameters": {
    "packageName": "com.example.app",
    "editId": "your-existing-edit-id",
    "language": "en-US"
  }
}
```

Configure `com.example.app` in the local app allowlist, or select it and Publisher
access during your self-hosted connection. Google still enforces the caller's
permissions and edit access. An edit ID never selects another customer's credential.

## Supported reads

All method names below have the prefix `androidpublisher.edits.`.

| Method | Additional required parameters | Result |
| --- | --- | --- |
| `get` | None | Edit ID and expiry |
| `details.get` | None | App details and default language |
| `listings.get` | `language` | One localized listing |
| `listings.list` | None | Localized listings |
| `images.list` | `language`, `imageType` | Image metadata and URLs |
| `tracks.get` | `track` | One track's edit state |
| `tracks.list` | None | Tracks in this edit |
| `countryavailability.get` | `track` | Country availability in this edit |
| `bundles.list` | None | Bundle metadata |
| `apks.list` | None | APK metadata |

All ten require `packageName` and `editId`. They are GET operations without a
request body or pagination parameters in the pinned Discovery definitions.
Image URLs are returned as data and are never fetched. Provider text and URLs are
untrusted content, not instructions to your assistant.

Use an image type from `describe_api_method`, such as `phoneScreenshots`; the
unspecified image type is not usable. Standard BCP-47 languages such as `zh-Hant-TW`
and form-factor tracks such as `wear:production` are supported. Do not URL-encode
path values yourself; the tool encodes individual segments.

This version accepts ASCII letters, digits, dot, underscore and hyphen in path
identifiers, plus colon in track names. Edit IDs are limited to 1,024 characters,
packages to 255, and other segments to 100. Empty and dot-only traversal segments
are rejected. These are local support limits, not Google-declared maxima.
[Google track names](https://developers.google.com/android-publisher/tracks#ff-track-name).

## Interpreting results and failures

Preserve version-code strings, rollout fractions, release notes, country data,
empty results and omitted fields. Fetch time is not publication time. Use
`list_releases` for release lifecycle information outside an edit; do not infer
publication from a track's draft, halted or in-progress contents.

An expired, invalidated or inaccessible edit produces a provider error. The tool
never retries by creating a new edit. Resume the existing publishing workflow and
provide its valid edit ID when appropriate. No automatic mutation or production
write is part of this inspection feature.

Contract, transport and synthetic MCP tests establish local implementation.
These methods have not yet been validated against a live Google edit.
