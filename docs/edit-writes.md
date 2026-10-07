# Existing-edit resource writes

`prepare_edit_write` and `apply_edit_write` support 13 explicitly selected Google
operations inside a caller-owned edit. Local use is disabled by default. Published and controlled-deployed
0.19.0 exposes the 11 ordinary methods through `play:edit-resources` and the two tester methods through separate
`play:tester-audience` consent. Existing connections gain no authority automatically.
See [hosted consent and limits](hosted-capabilities.md). These tools never create,
validate, commit or discard an edit, upload bytes or retry a mutation.

## Enable only the intended local methods

Add both settings to the local [Codex, Claude Code or generic harness](clients.md)
environment and restart the MCP process. This example enables app-detail PATCH only:

```text
GOOGLE_PLAY_PACKAGES=com.example.app
GOOGLE_PLAY_EDIT_WRITE_PACKAGES=com.example.app
GOOGLE_PLAY_EDIT_WRITE_METHODS=androidpublisher.edits.details.patch
```

Edit-write packages must be a subset of read packages. The method setting accepts
comma-separated **exact IDs** from the table below; wildcards, prefixes, unsupported
methods and future methods are rejected. Both settings are required to expose the
two tools. Existing listing, track, lifecycle and sensitive-read settings grant
none of this access. Google permissions still apply separately.

Authorization for a method includes its necessary baseline and readback GETs.
Tester preparations and observations intentionally return Google Group addresses
to the client; protect this data. This ancillary read does not grant the general
sensitive-read tool or access to orders and purchases.

## Supported operations and complete baselines

All method names in this table have the prefix `androidpublisher.edits.`.

| Methods | Preparation, preflight and readback | Effect |
| --- | --- | --- |
| `details.patch`, `details.update` | `details.get` | Change app details, default language and support contacts. |
| `expansionfiles.patch`, `expansionfiles.update` | `expansionfiles.get` | Reference another APK's existing expansion file; no upload. |
| `images.delete`, `images.deleteall` | `images.list` for the language and image type | Delete one image or that entire image collection. |
| `listings.delete`, `listings.deleteall`, `listings.update` | Complete `listings.list` collection | Delete one/all localized listings, or create/update a localized listing. |
| `testers.patch`, `testers.update` | `testers.get` for the track | Submit the complete intended Google Groups array, affecting tester access. |
| `tracks.create` | Complete `tracks.list` collection | Create an absent closed testing track, without releases. |
| `tracks.patch` | `tracks.get` | Submit the complete intended releases array for the track. |

All calls require an explicit `packageName` and `editId`, plus the method-specific
path parameters shown by `describe_api_method`. The server encodes bounded path
segments, including colon-qualified track names, and rejects unknown parameters.
URLs, image bytes and artifact links in provider data are never followed.

Collection baselines must contain an explicit, correctly typed `listings`, `images`
or `tracks` array with unique resource identities. An explicit `[]` can establish
absence. A missing field, `{}`, `null`, HTTP 204 or GET error cannot establish
absence and fails preparation/preflight. Track creation requires the requested
track to be absent. Listing PUT can create a locale absent from a validated list.
These are conservative local support limits, not a claim that Google always emits
empty collection fields.

## Exact input boundaries

These local rules are stricter than Google's generated schema. Inspect the
prepared request: omitted fields are never copied from the current resource.

- **App details:** PATCH requires a nonempty subset of `contactEmail`,
  `contactPhone`, `contactWebsite`, `defaultLanguage`. PUT requires all four.
  Values must be strings; `defaultLanguage` must be a nonempty bounded locale segment.
- **Expansion files:** body must be exactly `{"referencesVersion": 123}` with a
  positive JSON integer up to `2147483647`. The target `apkVersionCode` has the same
  integer rule. Choose `main` or `patch`; request `fileSize` is rejected.
- **Deletes:** omit `body`; the HTTP request has no body. Image type must be a
  supported explicit type, not `appImageTypeUnspecified`.
- **Listing PUT:** require `language`, `title`, `shortDescription` and
  `fullDescription`, with optional `video`. Language must match the path exactly.
  Empty text, newlines and exact Unicode are preserved. Omitted video is not retained
  by a hidden merge. Google text limits and content policies are not locally verified.
- **Testers:** body must contain only a complete `googleGroups` array. `[]` is an
  intentional clearing proposal. The local limit is 1000 distinct email-shaped
  strings of at most 320 characters without whitespace or control characters.
  These identify Google Groups; arbitrary tester email membership is unsupported.
  The server does not establish that an address is a real Google Group.
- **Track creation:** supply exactly `track`, `type: "CLOSED_TESTING"` and
  `formFactor`. Supported form factors are `DEFAULT` without a colon prefix,
  `WEAR` with `wear:`, and `AUTOMOTIVE` with `automotive:`. The alias must be nonempty;
  no additional prefix or colon is accepted.
- **Track PATCH:** supply exactly the matching `track` and a complete `releases`
  array. The [track staging input rules](track-staging.md#local-input-checks) apply,
  including explicit statuses, version-code strings and rollout fraction rules.
  `[]` is explicit clearing intent. Release names and version codes are never used
  as merge keys, and no entries are automatically appended or retained.

Requests and retained resource/edit snapshots are bounded to 64 KiB of serialized
JSON. Null resource-field values, unknown request fields, non-finite numbers and invalid Unicode
are rejected. Provider response omissions and unknown fields are retained where
compatible with the checked resource shape; required identities are validated.

## Prepare, inspect, apply

Call `prepare_edit_write` with the exact selected method and an effects acknowledgement:

```json
{
  "method": "androidpublisher.edits.details.patch",
  "parameters": {
    "packageName": "com.example.app",
    "editId": "your-existing-edit-id"
  },
  "body": {"contactEmail": "support@example.com"},
  "acknowledge_effects": true
}
```

Preparation reads edit identity/expiry and the full target-resource baseline. It
returns `before`, the exact `proposed_request`, `effects` and an opaque
`edit_write_` operation ID. It makes no mutation. Acknowledgement expresses intent;
it is not proof of human review or Google approval.

Inspect the full response, then call `apply_edit_write`:

```json
{
  "operation_id": "COPY_THE_RETURNED_ID",
  "confirmation": "COPY_THE_RETURNED_ID"
}
```

Preparations retain immutable request copies and the original credential in memory.
They are single-use, last at most ten minutes and never exceed observed edit expiry.
There are at most 100 preparations for this service. Restart loses them; expired
entries are pruned on later preparation, not by a timed memory-erasure guarantee.

Apply consumes the ID before preflight, rechecks method/app authorization, compares
the full resource and edit-metadata snapshots and dispatches the selected mutation
once. Even an apparently unchanged explicit action is dispatched. It then performs
one fresh baseline GET with the same credential. It never refreshes into another
account. The shared publishing lock covers preflight, mutation and readback.

## Read the two outcomes separately

| Result | Meaning and next step |
| --- | --- |
| `mutation_succeeded: true`, `readback_succeeded: true` | The response passed mutation checks and a fresh observation is available in `after`. `mutation_response` retains the actual mutation response separately. |
| `mutation_succeeded: true`, `readback_succeeded: false` | The mutation succeeded, but observation failed. `observation_error` says so explicitly; `after` is absent. Inspect the edit before preparing another action. |
| An “outcome unknown” error | The mutation was dispatched but its outcome could not be established. No readback or automatic retry follows. Reconcile state before another action. |
| A preflight error | No mutation was dispatched by this apply; the consumed ID cannot be reused. Resolve the scope, expiry, drift or provider failure before preparing again. |

Deletion responses may be empty; image delete-all may instead return `deleted`
image metadata. A successful deletion with readback includes `deletion_observed`;
track creation includes `creation_observed`. These may be false if observed state
does not show the intended effect, including races with another editor. They do
not overwrite the confirmed mutation result. Failed observation never says that
no write occurred.

PATCH array merging is not inferred from the method name. `before` and the exact
body express the observed state and caller's intent; only `after` is the fresh
observation. Google may normalize values or return fallback releases. External
changes can occur after preflight or before readback: this is not atomic
compare-and-swap. The lock does not coordinate other processes or Play Console.
Listing metadata does not inventory associated images or every edit resource.

No automatic rollback, replacement edit, retry or commit is performed. Staged
changes are not verified publication. Use the separately enabled
[edit lifecycle](edit-lifecycle.md) when you intend a whole-edit operation.

## Evidence and sources

Engineering and independent QA use synthetic provider responses and real MCP
stdio. No Google mutation was used for QA; live write acceptance remains unverified.
See [verification](validation.md) and the release PR for completed checks.

Primary contracts: [app details](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.details),
[expansion files](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.expansionfiles),
[image deletion](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.images/deleteall),
[listing update](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.listings/update),
[testers](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.testers),
[track creation](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks/create),
[track PATCH](https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks/patch).
